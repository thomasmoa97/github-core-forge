"""In-memory forge state: repositories, refs, issues, pull requests, labels.

All git objects live in one ObjectStore shared by every repo (objects are
content-addressed, so sharing is safe). Metadata (issues/PRs/labels) is kept
per repo. IDs are opaque and stable across reads.
"""

import base64
import hashlib
import itertools
import threading

from . import gitobj

VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
STRANGER_LOGIN = "olgabauer"
DEFAULT_BRANCH = "main"
# Fixed author/committer identity for server-made commits (deterministic).
SERVER_AUTHOR = "ada iversen <adaiversen@example.invalid> 1700000000 +0000"
MERGED_AT = "2025-02-10T08:19:57Z"


def _opaque(prefix, key):
    digest = hashlib.sha1(key.encode("utf-8")).digest()[:12]
    return prefix + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class Issue:
    __slots__ = ("number", "title", "body", "state", "labels", "seq", "comments", "gid")

    def __init__(self, number, title, body, seq, gid):
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"
        self.labels = []
        self.seq = seq
        self.comments = []  # list of (id, body)
        self.gid = gid


class Pull:
    __slots__ = ("number", "title", "body", "head", "base", "head_sha", "base_sha",
                 "state", "merged", "merged_at", "merge_sha", "seq", "gid", "files")

    def __init__(self, number, title, body, head, base, head_sha, base_sha, seq, gid, files):
        self.number = number
        self.title = title
        self.body = body
        self.head = head
        self.base = base
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.state = "open"
        self.merged = False
        self.merged_at = ""
        self.merge_sha = ""
        self.seq = seq
        self.gid = gid
        # files: list of (path, status, additions, deletions)
        self.files = files


class Repo:
    __slots__ = ("name", "owner", "description", "private", "default_branch",
                 "db_id", "gid", "refs", "issues", "pulls", "labels",
                 "next_issue", "next_pr", "next_comment", "seq")

    def __init__(self, name, owner, description, private, db_id, seq):
        self.name = name
        self.owner = owner
        self.description = description
        self.private = private
        self.default_branch = DEFAULT_BRANCH
        self.db_id = db_id
        self.gid = _opaque("R_", "repo:%s/%s" % (owner, name))
        self.refs = {}  # "refs/heads/main" -> oid ; "refs/tags/v1" -> oid
        self.issues = {}
        self.pulls = {}
        self.labels = {}  # name -> {"name":..., "color":...}
        self.next_issue = 1
        self.next_pr = 1
        self.next_comment = 1
        self.seq = seq


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.objects = gitobj.ObjectStore()
        self.repos = {}  # (owner, name) -> Repo
        self._next_db_id = itertools.count(5000)
        self._seq = itertools.count(1)
        self._req = itertools.count(1)

    def next_req_id(self):
        return "req-%d" % next(self._req)

    def create_repo(self, owner, name, description, private):
        with self.lock:
            key = (owner, name)
            if key in self.repos:
                return None
            repo = Repo(name, owner, description, private, next(self._next_db_id), next(self._seq))
            self.repos[key] = repo
            return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None)

    def list_repos(self, owner):
        return sorted((r for (o, _n), r in self.repos.items() if o == owner),
                      key=lambda r: r.seq)

    # ---- git helpers -------------------------------------------------

    def ref(self, repo, refname):
        return repo.refs.get(refname)

    def set_ref(self, repo, refname, oid):
        with self.lock:
            if oid == gitobj.ZERO_OID:
                repo.refs.pop(refname, None)
            else:
                repo.refs[refname] = oid

    def head_ref(self, repo):
        return "refs/heads/" + repo.default_branch

    def head_oid(self, repo):
        return repo.refs.get(self.head_ref(repo))

    def commit_for_ref(self, repo, refname):
        oid = repo.refs.get(refname)
        if not oid:
            return None
        got = self.objects.get(oid)
        if got and got[0] == "commit":
            return oid
        if got and got[0] == "tag":
            return gitobj.parse_tag(got[1])["object"]
        return None

    def resolve_ref(self, repo, ref):
        """Resolve a branch name, full ref, or SHA to a commit oid."""
        if ref in repo.refs:
            return self.commit_for_ref(repo, ref)
        full = "refs/heads/" + ref
        if full in repo.refs:
            return self.commit_for_ref(repo, full)
        got = self.objects.get(ref)
        if got and got[0] == "commit":
            return ref
        return None

    def tree_oid_at(self, repo, commit_oid):
        got = self.objects.get(commit_oid)
        if not got or got[0] != "commit":
            return None
        return gitobj.parse_commit(got[1])["tree"]

    def read_path(self, repo, commit_oid, path):
        """Return (mode, oid, payload) for a blob at path in a commit, or None."""
        tree = self.tree_oid_at(repo, commit_oid)
        if tree is None:
            return None
        files = gitobj.flatten_tree(self.objects, tree)
        hit = files.get(path)
        if hit is None:
            return None
        mode, oid = hit
        got = self.objects.get(oid)
        if got is None:
            return None
        return (mode, oid, got[1])

    def list_dir(self, repo, commit_oid, path):
        """Entries directly under path ('' for root): list of (type, name, fullpath, oid)."""
        tree = self.tree_oid_at(repo, commit_oid)
        if tree is None:
            return None
        if not path:
            got = self.objects.get(tree)
            if got is None:
                return []
            out = []
            for mode, name, oid in gitobj.parse_tree(got[1]):
                out.append(("dir" if mode == "40000" else "file", name, name, oid))
            return out
        files = gitobj.flatten_tree(self.objects, tree)
        prefix = path.rstrip("/") + "/"
        seen = {}
        for p, (mode, oid) in files.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            first = rest.split("/")[0]
            if "/" in rest:
                seen[first] = ("dir", first, prefix + first, None)
            else:
                seen[first] = ("file", first, p, oid)
        return sorted(seen.values(), key=lambda e: e[1])

    def commit_file(self, repo, branch, path, content, message, committer=None):
        """Create a commit on branch setting path to content (bytes).
        Returns the new commit oid. Creates the branch if unborn."""
        with self.lock:
            refname = "refs/heads/" + branch
            parent = repo.refs.get(refname)
            if parent:
                tree = self.tree_oid_at(repo, parent)
                files = gitobj.flatten_tree(self.objects, tree) if tree else {}
                parents = [parent]
            else:
                files = {}
                parents = []
            blob = self.objects.put("blob", content)
            files[path] = ("100644", blob)
            new_tree = gitobj.build_tree_from_files(self.objects, files)
            who = committer or SERVER_AUTHOR
            raw = gitobj.build_commit(new_tree, parents, who, who, message)
            commit = self.objects.put("commit", raw)
            repo.refs[refname] = commit
            return commit

    def delete_file(self, repo, branch, path, message):
        """Create a commit on branch removing path. Returns new commit oid or None."""
        with self.lock:
            refname = "refs/heads/" + branch
            parent = repo.refs.get(refname)
            if not parent:
                return None
            tree = self.tree_oid_at(repo, parent)
            files = gitobj.flatten_tree(self.objects, tree) if tree else {}
            if path not in files:
                return None
            del files[path]
            new_tree = gitobj.build_tree_from_files(self.objects, files)
            raw = gitobj.build_commit(new_tree, [parent], SERVER_AUTHOR, SERVER_AUTHOR, message)
            commit = self.objects.put("commit", raw)
            repo.refs[refname] = commit
            return commit

    # ---- issues ------------------------------------------------------

    def create_issue(self, repo, title, body, labels):
        with self.lock:
            n = repo.next_issue
            repo.next_issue += 1
            gid = _opaque("I_", "issue:%s/%s#%d" % (repo.owner, repo.name, n))
            issue = Issue(n, title, body, next(self._seq), gid)
            issue.labels = list(labels)
            repo.issues[n] = issue
            return issue

    def create_pull(self, repo, title, body, head, base, head_sha, base_sha, files):
        with self.lock:
            n = repo.next_pr
            repo.next_pr += 1
            gid = _opaque("PR_", "pr:%s/%s#%d" % (repo.owner, repo.name, n))
            pr = Pull(n, title, body, head, base, head_sha, base_sha, next(self._seq), gid, files)
            repo.pulls[n] = pr
            return pr

    def add_comment(self, repo, issue, body):
        with self.lock:
            cid = repo.next_comment
            repo.next_comment += 1
            issue.comments.append((cid, body))
            return cid

    # ---- merge -------------------------------------------------------

    def merge_conflict(self, repo, pr):
        """True if squashing head onto base conflicts (same path changed both ways)."""
        head_tip = pr.head_sha
        # The CURRENT base tip, not pr.base_sha: pr.base_sha is the base at
        # creation time, but an earlier PR may have advanced the base branch
        # since (the merge-conflict check merges PR #1, then expects PR #2
        # to conflict against the NEW base).
        base_tip = repo.refs.get("refs/heads/" + pr.base)
        if base_tip is None:
            base_tip = pr.base_sha
        if head_tip == base_tip:
            return False
        base = gitobj.merge_base(self.objects, head_tip, base_tip)
        if base is None:
            return False
        base_files = gitobj.flatten_tree(self.objects, self.tree_oid_at(repo, base)) if self.tree_oid_at(repo, base) else {}
        head_files = gitobj.flatten_tree(self.objects, self.tree_oid_at(repo, head_tip)) if self.tree_oid_at(repo, head_tip) else {}
        base_branch_files = gitobj.flatten_tree(self.objects, self.tree_oid_at(repo, base_tip)) if self.tree_oid_at(repo, base_tip) else {}
        for path in set(head_files) | set(base_branch_files):
            h = head_files.get(path)
            b = base_branch_files.get(path)
            o = base_files.get(path)
            if h != o and b != o and h != b:
                return True
        return False

    def squash_merge(self, repo, pr):
        """Perform the squash merge. Returns merge commit oid or None on conflict."""
        with self.lock:
            if pr.state != "open":
                return None
            if self.merge_conflict(repo, pr):
                return None
            base_tip = repo.refs.get("refs/heads/" + pr.base)
            if base_tip is None:
                return None
            head_tree = self.tree_oid_at(repo, pr.head_sha)
            if head_tree is None:
                return None
            raw = gitobj.build_commit(head_tree, [base_tip], SERVER_AUTHOR, SERVER_AUTHOR,
                                      pr.title + "\n\n" + pr.body if pr.body else pr.title)
            merge_oid = self.objects.put("commit", raw)
            repo.refs["refs/heads/" + pr.base] = merge_oid
            pr.state = "closed"
            pr.merged = True
            pr.merged_at = MERGED_AT
            pr.merge_sha = merge_oid
            return merge_oid
