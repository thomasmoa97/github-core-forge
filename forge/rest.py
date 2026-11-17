"""REST v3 API under /api/v3. Auth is checked by the HTTP layer.

Every reply is application/json with X-GitHub-Request-Id (set by the layer).
Errors are {"message": "..."}. Unknown routes are 404 JSON. Malformed JSON
bodies are 400.
"""

import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from . import gitobj
from .store import DEFAULT_BRANCH, STRANGER_LOGIN, VIEWER_EMAIL, VIEWER_ID, VIEWER_LOGIN, VIEWER_NAME

SCOPES = "repo, read:org"


def _repo_json(repo):
    return {
        "id": repo.db_id,
        "node_id": repo.gid,
        "name": repo.name,
        "full_name": "%s/%s" % (repo.owner, repo.name),
        "owner": {"login": repo.owner, "id": VIEWER_ID if repo.owner == VIEWER_LOGIN else 1002,
                  "type": "User"},
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "html_url": "https://github.com/%s/%s" % (repo.owner, repo.name),
    }


def _issue_json(issue):
    return {
        "id": issue.gid,
        "node_id": issue.gid,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": [{"name": n} for n in issue.labels],
        "user": {"login": VIEWER_LOGIN},
    }


def _pull_json(pr):
    out = {
        "id": pr.gid,
        "node_id": pr.gid,
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "head": {"ref": pr.head, "sha": pr.head_sha,
                 "label": "%s:%s" % (VIEWER_LOGIN, pr.head)},
        "base": {"ref": pr.base, "sha": pr.base_sha,
                 "label": "%s:%s" % (VIEWER_LOGIN, pr.base)},
        "merged": pr.merged,
    }
    if pr.merged:
        out["merged_at"] = pr.merged_at
        out["merge_commit_sha"] = pr.merge_sha
    return out


def _diff_files(store, repo, base_tip, head_tip):
    """Changed files between two commits: list of (path, status, add, del)."""
    def flat(tip):
        if not tip:
            return {}
        tree = store.tree_oid_at(repo, tip)
        return gitobj.flatten_tree(store.objects, tree) if tree else {}
    old = flat(base_tip)
    new = flat(head_tip)
    out = []
    for path in sorted(set(old) | set(new)):
        o = old.get(path)
        n = new.get(path)
        if o is None:
            content = store.objects.get(n[1])[1]
            adds = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
            out.append((path, "added", adds, 0))
        elif n is None:
            content = store.objects.get(o[1])[1]
            dels = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
            out.append((path, "removed", 0, dels))
        elif o[1] != n[1]:
            old_lines = set(store.objects.get(o[1])[1].split(b"\n"))
            new_lines = set(store.objects.get(n[1])[1].split(b"\n"))
            adds = len(new_lines - old_lines)
            dels = len(old_lines - new_lines)
            out.append((path, "modified", adds, dels))
    return out


class Ctx:
    def __init__(self, forge, handler, path):
        self.forge = forge
        self.h = handler
        self.store = forge.store
        self.query = parse_qs(urlparse(handler.path).query)
        self.path = path

    def q(self, name, default=None):
        vals = self.query.get(name)
        return vals[0] if vals else default

    def body(self):
        raw = self.h._read_body()
        if raw is None:
            return None, "body too large"
        if not raw:
            return {}, None
        try:
            return json.loads(raw.decode("utf-8")), None
        except Exception:
            return None, "malformed JSON"

    def send(self, status, obj, extra=None):
        self.h._send_json(status, obj, extra)

    def err(self, status, msg):
        self.h._send_json(status, {"message": msg})

    def base_url(self):
        host = self.h.headers.get("Host", "127.0.0.1")
        return "http://" + host

    def page_link(self, path, per_page, page):
        url = "%s%s?per_page=%d&page=%d" % (self.base_url(), path, per_page, page)
        return '<%s>; rel="next"' % url

    def paginate(self, items, path):
        per_page = int(self.q("per_page", "30") or 30)
        page = int(self.q("page", "1") or 1)
        per_page = max(1, min(per_page, 100))
        page = max(1, page)
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        extra = {}
        if start + per_page < len(items):
            extra["Link"] = self.page_link(path, per_page, page + 1)
        return chunk, extra


def _lookup(ctx, owner, name):
    """Repo visible to the viewer: 404 for missing repos and other owners."""
    if owner != VIEWER_LOGIN:
        return None
    return ctx.store.get_repo(owner, name)


def handle(forge, handler, path):
    ctx = Ctx(forge, handler, path)
    try:
        _dispatch(ctx)
    except (BrokenPipeError, ConnectionResetError):
        raise
    except Exception as exc:
        try:
            ctx.err(500, "Internal Server Error: %s" % exc)
        except Exception:
            pass


def _dispatch(ctx):
    h = ctx.h
    method = h.command
    sub = ctx.path[len("/api/v3"):] or "/"
    parts = [unquote(p) for p in sub.strip("/").split("/") if p != ""]

    # --- API root / meta / user -------------------------------------
    if sub == "/" and method == "GET":
        ctx.send(200, {"current_user_url": "/api/v3/user",
                       "repository_url": "/api/v3/repos/{owner}/{repo}"},
                 {"X-OAuth-Scopes": SCOPES})
        return
    if sub == "/meta" and method == "GET":
        ctx.send(200, {"installed_version": "1.0.0"})
        return
    if sub == "/user" and method == "GET":
        ctx.send(200, {"login": VIEWER_LOGIN, "id": VIEWER_ID, "name": VIEWER_NAME,
                       "email": VIEWER_EMAIL, "type": "User"})
        return
    if parts == ["user", "repos"]:
        if method == "GET":
            repos = ctx.store.list_repos(VIEWER_LOGIN)
            chunk, extra = ctx.paginate([_repo_json(r) for r in repos], "/api/v3/user/repos")
            ctx.send(200, chunk, extra)
            return
        if method == "POST":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            name = (body or {}).get("name") or ""
            if not name:
                ctx.err(422, "name is required")
                return
            if ctx.store.get_repo(VIEWER_LOGIN, name):
                ctx.err(422, "Repository already exists")
                return
            repo = ctx.store.create_repo(VIEWER_LOGIN, name,
                                         (body or {}).get("description") or "",
                                         bool((body or {}).get("private", False)))
            ctx.send(201, _repo_json(repo))
            return

    # --- /repos/{owner}/{repo}/... ----------------------------------
    if len(parts) >= 3 and parts[0] == "repos":
        owner, name = parts[1], parts[2]
        repo = _lookup(ctx, owner, name)
        rest = parts[3:]
        if repo is None:
            ctx.err(404, "Not Found")
            return
        _repo_routes(ctx, repo, rest, method)
        return

    ctx.err(404, "Not Found")


def _repo_routes(ctx, repo, rest, method):
    store = ctx.store
    h = ctx.h

    if not rest:
        if method == "GET":
            ctx.send(200, _repo_json(repo))
            return
        if method == "PATCH":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            if body and "description" in body:
                repo.description = body["description"]
            ctx.send(200, _repo_json(repo))
            return
        if method == "DELETE":
            store.delete_repo(repo.owner, repo.name)
            h._send_bytes(204, b"", "application/json")
            return
        ctx.err(404, "Not Found")
        return

    head = rest[0]

    if head == "branches":
        if len(rest) == 1 and method == "GET":
            out = []
            for refname in sorted(repo.refs):
                if refname.startswith("refs/heads/"):
                    out.append({"name": refname[len("refs/heads/"):],
                                "commit": {"sha": repo.refs[refname]}})
            ctx.send(200, out)
            return
        if len(rest) == 2 and method == "GET":
            refname = "refs/heads/" + rest[1]
            oid = repo.refs.get(refname)
            if oid is None:
                ctx.err(404, "Not Found")
                return
            ctx.send(200, {"name": rest[1], "commit": {"sha": oid}})
            return
        ctx.err(404, "Not Found")
        return

    if head == "git" and len(rest) >= 2 and rest[1] == "refs":
        if method == "POST" and len(rest) == 2:
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            ref = (body or {}).get("ref") or ""
            sha = (body or {}).get("sha") or ""
            if not ref.startswith("refs/") or not sha:
                ctx.err(422, "ref and sha are required")
                return
            if ref in repo.refs:
                ctx.err(422, "Reference already exists")
                return
            got = store.objects.get(sha)
            if got is None or got[0] != "commit":
                ctx.err(422, "Invalid sha")
                return
            repo.refs[ref] = sha
            ctx.send(201, {"ref": ref, "object": {"sha": sha}})
            return
        if method == "GET" and len(rest) >= 3:
            refname = "/".join(rest[2:])
            oid = repo.refs.get(refname)
            if oid is None:
                ctx.err(404, "Not Found")
                return
            ctx.send(200, {"ref": refname, "object": {"sha": oid}})
            return
        ctx.err(404, "Not Found")
        return

    if head == "commits" and method == "GET" and len(rest) == 1:
        sha = ctx.q("sha")
        tip = None
        if sha:
            tip = store.resolve_ref(repo, sha)
        else:
            tip = store.head_oid(repo)
        out = []
        seen = set()
        stack = [tip] if tip else []
        while stack:
            oid = stack.pop(0)
            if oid in seen or oid == gitobj.ZERO_OID:
                continue
            seen.add(oid)
            got = store.objects.get(oid)
            if got is None or got[0] != "commit":
                continue
            c = gitobj.parse_commit(got[1])
            out.append({"sha": oid, "commit": {"message": c["message"]}})
            stack.extend(c["parents"])
        ctx.send(200, out)
        return

    if head == "contents":
        _contents(ctx, repo, rest[1:], method)
        return

    if head == "labels":
        if method == "POST" and len(rest) == 1:
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            name = (body or {}).get("name") or ""
            if not name:
                ctx.err(422, "name is required")
                return
            repo.labels[name] = {"name": name, "color": (body or {}).get("color", "ededed")}
            ctx.send(201, {"name": name})
            return
        if method == "GET" and len(rest) == 1:
            ctx.send(200, [{"name": n} for n in repo.labels])
            return
        ctx.err(404, "Not Found")
        return

    if head == "issues":
        _issues(ctx, repo, rest[1:], method)
        return

    if head == "pulls":
        _pulls(ctx, repo, rest[1:], method)
        return

    ctx.err(404, "Not Found")


def _contents(ctx, repo, rest, method):
    store = ctx.store
    if not rest:
        ctx.err(404, "Not Found")
        return
    path = "/".join(rest)
    ref = ctx.q("ref")
    if method == "GET":
        commit = store.resolve_ref(repo, ref) if ref else store.head_oid(repo)
        if commit is None:
            ctx.err(404, "Not Found")
            return
        hit = store.read_path(repo, commit, path)
        if hit is not None:
            _mode, oid, payload = hit
            ctx.send(200, {"type": "file", "encoding": "base64", "size": len(payload),
                           "name": path.rsplit("/", 1)[-1], "path": path, "sha": oid,
                           "content": base64.b64encode(payload).decode("ascii")})
            return
        entries = store.list_dir(repo, commit, path)
        if entries is None:
            ctx.err(404, "Not Found")
            return
        out = []
        for typ, name, full, oid in entries:
            e = {"type": typ, "name": name, "path": full}
            if oid:
                e["sha"] = oid
            out.append(e)
        # Distinguish "missing path" from "empty dir": if the path is neither
        # a file nor a prefix of any file, it is missing.
        if not out:
            tree = store.tree_oid_at(repo, commit)
            files = gitobj.flatten_tree(store.objects, tree) if tree else {}
            if not any(p == path or p.startswith(path.rstrip("/") + "/") for p in files):
                ctx.err(404, "Not Found")
                return
        ctx.send(200, out)
        return
    if method == "PUT":
        body, err = ctx.body()
        if err:
            ctx.err(400, err)
            return
        body = body or {}
        try:
            content = base64.b64decode(body.get("content", ""), validate=False)
        except Exception:
            ctx.err(422, "content must be base64")
            return
        branch = body.get("branch") or repo.default_branch
        message = body.get("message") or "Update %s" % path
        refname = "refs/heads/" + branch
        parent = repo.refs.get(refname)
        existed = False
        if parent:
            hit = store.read_path(repo, parent, path)
            if hit is not None:
                existed = True
                if body.get("sha") and body["sha"].lower() != hit[1].lower():
                    ctx.err(409, "sha does not match the current blob")
                    return
        commit = store.commit_file(repo, branch, path, content, message)
        blob = store.objects.put("blob", content)
        ctx.send(201 if not existed else 200,
                 {"content": {"sha": blob, "encoding": "base64"},
                  "commit": {"sha": commit}})
        return
    if method == "DELETE":
        body, err = ctx.body()
        if err:
            ctx.err(400, err)
            return
        body = body or {}
        branch = body.get("branch") or repo.default_branch
        refname = "refs/heads/" + branch
        parent = repo.refs.get(refname)
        if not parent:
            ctx.err(404, "Not Found")
            return
        hit = store.read_path(repo, parent, path)
        if hit is None:
            ctx.err(404, "Not Found")
            return
        if body.get("sha") and body["sha"].lower() != hit[1].lower():
            ctx.err(409, "sha does not match the current blob")
            return
        commit = store.delete_file(repo, branch, path, body.get("message") or "Delete %s" % path)
        if commit is None:
            ctx.err(404, "Not Found")
            return
        ctx.send(200, {"commit": {"sha": commit}})
        return
    ctx.err(404, "Not Found")


def _issues(ctx, repo, rest, method):
    store = ctx.store
    if not rest:
        if method == "POST":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            body = body or {}
            labels = body.get("labels") or []
            for name in labels:
                if name not in repo.labels:
                    ctx.err(422, "Label '%s' does not exist" % name)
                    return
            issue = store.create_issue(repo, body.get("title") or "",
                                       body.get("body") or "", labels)
            ctx.send(201, _issue_json(issue))
            return
        if method == "GET":
            state = ctx.q("state", "open")
            sort = ctx.q("sort", "created")
            direction = ctx.q("direction", "desc")
            issues = list(repo.issues.values())
            if state != "all":
                issues = [i for i in issues if i.state == state]
            reverse = (direction == "desc")
            issues.sort(key=lambda i: i.seq, reverse=reverse)
            chunk, extra = ctx.paginate([_issue_json(i) for i in issues],
                                        "/api/v3/repos/%s/%s/issues" % (repo.owner, repo.name))
            ctx.send(200, chunk, extra)
            return
        ctx.err(404, "Not Found")
        return
    try:
        number = int(rest[0])
    except ValueError:
        ctx.err(404, "Not Found")
        return
    issue = repo.issues.get(number)
    if issue is None:
        ctx.err(404, "Not Found")
        return
    if len(rest) == 1:
        if method == "GET":
            ctx.send(200, _issue_json(issue))
            return
        if method == "PATCH":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            if body and "state" in body:
                issue.state = body["state"]
            if body and "title" in body:
                issue.title = body["title"]
            if body and "body" in body:
                issue.body = body["body"]
            ctx.send(200, _issue_json(issue))
            return
        ctx.err(404, "Not Found")
        return
    if rest[1] == "comments":
        if method == "POST":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            cid = store.add_comment(repo, issue, (body or {}).get("body") or "")
            ctx.send(201, {"id": cid, "body": (body or {}).get("body") or ""})
            return
        if method == "GET":
            ctx.send(200, [{"id": cid, "body": b} for cid, b in issue.comments])
            return
    ctx.err(404, "Not Found")


def _pulls(ctx, repo, rest, method):
    store = ctx.store
    if not rest:
        if method == "POST":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            body = body or {}
            head = body.get("head") or ""
            base = body.get("base") or repo.default_branch
            head_sha = store.resolve_ref(repo, head)
            base_sha = store.resolve_ref(repo, base)
            if head_sha is None or base_sha is None:
                ctx.err(422, "head or base branch not found")
                return
            files = _diff_files(store, repo, base_sha, head_sha)
            pr = store.create_pull(repo, body.get("title") or "", body.get("body") or "",
                                   head, base, head_sha, base_sha,
                                   [(p, s, a, d) for p, s, a, d in files])
            ctx.send(201, _pull_json(pr))
            return
        if method == "GET":
            state = ctx.q("state", "open")
            pulls = list(repo.pulls.values())
            if state != "all":
                pulls = [p for p in pulls if p.state == state]
            pulls.sort(key=lambda p: p.seq)
            chunk, extra = ctx.paginate([_pull_json(p) for p in pulls],
                                        "/api/v3/repos/%s/%s/pulls" % (repo.owner, repo.name))
            ctx.send(200, chunk, extra)
            return
        ctx.err(404, "Not Found")
        return
    try:
        number = int(rest[0])
    except ValueError:
        ctx.err(404, "Not Found")
        return
    pr = repo.pulls.get(number)
    if len(rest) == 2 and rest[1] == "merge":
        if pr is None:
            ctx.err(404, "Not Found")
            return
        if method == "PUT":
            body, err = ctx.body()
            if err:
                ctx.err(400, err)
                return
            if pr.state != "open":
                ctx.err(405, "Pull request is already closed")
                return
            merge_sha = store.squash_merge(repo, pr)
            if merge_sha is None:
                ctx.err(405, "Merge conflict")
                return
            ctx.send(200, {"merged": True, "sha": merge_sha,
                           "message": "Pull request successfully merged"})
            return
        ctx.err(404, "Not Found")
        return
    if pr is None:
        ctx.err(404, "Not Found")
        return
    if len(rest) == 1 and method == "GET":
        ctx.send(200, _pull_json(pr))
        return
    if len(rest) == 2 and rest[1] == "files" and method == "GET":
        out = [{"filename": p, "status": s, "additions": a, "deletions": d,
                "changes": a + d} for p, s, a, d in pr.files]
        ctx.send(200, out)
        return
    ctx.err(404, "Not Found")
