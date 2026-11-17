"""GraphQL v4: a small parser + resolver for exactly the documents gh and
github-mcp-server send. No external dependency.

Supports: query/mutation operations, aliases, variables, arguments (string,
int, bool, null, enum, list, object), inline fragments, __type
introspection, batched (array) request bodies.
"""

import base64
import json
import re

from . import rest as restmod
from .store import VIEWER_LOGIN

# ---------------------------------------------------------------- tokenizer

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+|,)|(?P<ellipsis>\.\.\.)|(?P<punct>[{}()\[\]:!@$=])|
    (?P<string>"(?:\\.|[^"\\])*")|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)


def _tokenize(src):
    toks = []
    i = 0
    while i < len(src):
        m = _TOKEN_RE.match(src, i)
        if not m:
            raise ValueError("unexpected character %r at %d" % (src[i], i))
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        toks.append((kind, m.group()))
    toks.append(("eof", ""))
    return toks


def _unquote(s):
    return json.loads(s)


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind, val=None):
        t = self.next()
        if t[0] != kind or (val is not None and t[1] != val):
            raise ValueError("expected %s %s, got %r" % (kind, val, t))
        return t

    def parse_document(self):
        ops = []
        while self.peek()[0] != "eof":
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        kind, val = self.peek()
        if kind == "punct" and val == "{":
            return {"kind": "query", "name": None, "vars": {}, "sel": self.parse_selection_set()}
        if kind == "name" and val in ("query", "mutation", "subscription"):
            self.next()
            name = None
            if self.peek()[0] == "name":
                name = self.next()[1]
            variables = {}
            if self.peek() == ("punct", "("):
                variables = self.parse_var_defs()
            self._skip_directives()
            return {"kind": val, "name": name, "vars": variables, "sel": self.parse_selection_set()}
        raise ValueError("expected operation, got %r" % (self.peek(),))

    def parse_var_defs(self):
        self.expect("punct", "(")
        out = {}
        while self.peek() != ("punct", ")"):
            self.expect("punct", "$")
            name = self.expect("name")[1]
            self.expect("punct", ":")
            out[name] = self.parse_type()
            if self.peek() == ("punct", "="):
                self.next()
                self.parse_value()
        self.expect("punct", ")")
        return out

    def parse_type(self):
        if self.peek() == ("punct", "["):
            self.next()
            t = self.parse_type()
            self.expect("punct", "]")
        else:
            t = self.expect("name")[1]
        if self.peek() == ("punct", "!"):
            self.next()
        return t

    def _skip_directives(self):
        while self.peek() == ("punct", "@"):
            self.next()
            self.expect("name")
            if self.peek() == ("punct", "("):
                self.parse_args()

    def parse_selection_set(self):
        self.expect("punct", "{")
        sels = []
        while self.peek() != ("punct", "}"):
            sels.append(self.parse_selection())
        self.expect("punct", "}")
        return sels

    def parse_selection(self):
        if self.peek()[0] == "ellipsis":
            self.expect("ellipsis")
            if self.peek() == ("name", "on"):
                self.next()
                type_name = self.expect("name")[1]
                self._skip_directives()
                return {"frag": type_name, "sel": self.parse_selection_set()}
            raise ValueError("named fragments are not supported")
        name = self.expect("name")[1]
        alias = None
        if self.peek() == ("punct", ":"):
            self.next()
            alias = name
            name = self.expect("name")[1]
        args = {}
        if self.peek() == ("punct", "("):
            args = self.parse_args()
        self._skip_directives()
        sel = None
        if self.peek() == ("punct", "{"):
            sel = self.parse_selection_set()
        return {"field": name, "alias": alias or name, "args": args, "sel": sel}

    def parse_args(self):
        self.expect("punct", "(")
        out = {}
        while self.peek() != ("punct", ")"):
            name = self.expect("name")[1]
            self.expect("punct", ":")
            out[name] = self.parse_value()
        self.expect("punct", ")")
        return out

    def parse_value(self):
        kind, val = self.next()
        if kind == "punct" and val == "$":
            return ("var", self.expect("name")[1])
        if kind == "string":
            return ("str", _unquote(val))
        if kind == "number":
            return ("int", int(val)) if re.fullmatch(r"-?\d+", val) else ("float", float(val))
        if kind == "name":
            if val == "true":
                return ("bool", True)
            if val == "false":
                return ("bool", False)
            if val == "null":
                return ("null", None)
            return ("enum", val)
        if kind == "punct" and val == "[":
            items = []
            while self.peek() != ("punct", "]"):
                items.append(self.parse_value())
            self.expect("punct", "]")
            return ("list", items)
        if kind == "punct" and val == "{":
            obj = {}
            while self.peek() != ("punct", "}"):
                name = self.expect("name")[1]
                self.expect("punct", ":")
                obj[name] = self.parse_value()
            self.expect("punct", "}")
            return ("obj", obj)
        raise ValueError("unexpected value token %r" % ((kind, val),))


def _resolve_value(node, variables):
    kind, val = node
    if kind == "var":
        return variables.get(val)
    if kind == "list":
        return [_resolve_value(v, variables) for v in val]
    if kind == "obj":
        return {k: _resolve_value(v, variables) for k, v in val.items()}
    return val


# ---------------------------------------------------------------- introspection

_INTROSPECTION = {
    "Issue": {"fields": ["id", "number", "title", "state", "body", "url", "createdAt",
                         "author", "labels", "comments"],
              "enum": None},
    "PullRequest": {"fields": ["id", "number", "title", "body", "state", "merged",
                               "mergeable", "headRefName", "baseRefName", "files",
                               "createdAt", "author"],
                    "enum": None},
    "Repository": {"fields": ["id", "databaseId", "name", "nameWithOwner", "owner",
                              "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                              "issue", "issues", "pullRequests", "pullRequest",
                              "description", "url", "createdAt"],
                   "enum": None},
    "SearchType": {"fields": [], "enum": ["ISSUE", "REPOSITORY", "USER"]},
    "IssueState": {"fields": [], "enum": ["OPEN", "CLOSED"]},
    "OrderDirection": {"fields": [], "enum": ["ASC", "DESC"]},
    "IssueOrderField": {"fields": [], "enum": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
}


def _cursor(seq):
    return base64.urlsafe_b64encode(("c:%d" % seq).encode()).decode().rstrip("=")


def _uncursor(cursor):
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        return int(raw.split(":")[1])
    except Exception:
        return None


# ---------------------------------------------------------------- execution

class Ctx:
    def __init__(self, forge, variables):
        self.forge = forge
        self.store = forge.store
        self.variables = variables or {}
        self.errors = []

    def err(self, message):
        self.errors.append({"message": message})


def _find_by_gid(store, gid):
    for repo in store.repos.values():
        if repo.gid == gid:
            return ("Repository", repo)
        for issue in repo.issues.values():
            if issue.gid == gid:
                return ("Issue", (repo, issue))
        for pr in repo.pulls.values():
            if pr.gid == gid:
                return ("PullRequest", (repo, pr))
    return None


def _issue_state(issue):
    return "OPEN" if issue.state == "open" else "CLOSED"


def _pr_state(pr):
    if pr.state == "open":
        return "OPEN"
    return "MERGED" if pr.merged else "CLOSED"


def _resolve_issue_connection(ctx, repo, args):
    first = args.get("first")
    states = args.get("states")
    order_by = args.get("orderBy") or {}
    after = args.get("after")
    issues = list(repo.issues.values())
    if states:
        wanted = set(s.upper() for s in states)
        issues = [i for i in issues if _issue_state(i) in wanted]
    direction = (order_by.get("direction") or "ASC").upper()
    issues.sort(key=lambda i: i.seq, reverse=(direction == "DESC"))
    if after:
        seq = _uncursor(after)
        if seq is not None:
            issues = [i for i in issues if i.seq > seq]
    total = len(issues)
    if first is not None:
        page = issues[:first]
        has_next = total > first
    else:
        page = issues
        has_next = False
    end = _cursor(page[-1].seq) if page else ""
    return {"__type": "IssueConnection",
            "nodes": [("Issue", i) for i in page],
            "pageInfo": {"__type": "PageInfo", "hasNextPage": has_next, "endCursor": end}}


def _execute(ctx, selections, type_name, obj):
    """Execute selections against (type_name, obj). Returns a dict."""
    out = {}
    for sel in selections:
        if "frag" in sel:
            # Inline fragment: apply only when the type matches (or no type).
            if sel["frag"] == type_name or type_name in ("Node",):
                out.update(_execute(ctx, sel["sel"], type_name, obj))
            continue
        field = sel["field"]
        alias = sel["alias"]
        args = {k: _resolve_value(v, ctx.variables) for k, v in sel["args"].items()}
        val, val_type = _resolve_field(ctx, type_name, obj, field, args)
        if val is _UNKNOWN:
            ctx.err('Cannot query field "%s" on type "%s".' % (field, type_name))
            continue
        if sel["sel"] is None:
            out[alias] = val
        else:
            if val is None:
                out[alias] = None
            elif isinstance(val, list):
                items = []
                for item in val:
                    if isinstance(item, tuple):
                        items.append(_execute(ctx, sel["sel"], item[0], item[1]))
                    elif isinstance(item, dict) and "__type" in item:
                        items.append(_execute(ctx, sel["sel"], item["__type"], item))
                    else:
                        items.append(item)
                out[alias] = items
            elif isinstance(val, tuple):
                out[alias] = _execute(ctx, sel["sel"], val[0], val[1])
            elif isinstance(val, dict) and "__type" in val:
                out[alias] = _execute(ctx, sel["sel"], val["__type"], val)
            else:
                out[alias] = val
    return out


class _Unknown:
    pass


_UNKNOWN = _Unknown()


def _resolve_field(ctx, type_name, obj, field, args):
    store = ctx.store
    if type_name == "Query":
        if field == "viewer":
            return ("User", {"login": VIEWER_LOGIN}), "User"
        if field == "repository":
            repo = store.get_repo(args.get("owner"), args.get("name"))
            if repo is None or repo.owner != VIEWER_LOGIN:
                return None, "Repository"
            return ("Repository", repo), "Repository"
        if field == "node":
            found = _find_by_gid(store, args.get("id"))
            if found is None:
                return None, "Node"
            return found, found[0]
        if field == "__type":
            info = _INTROSPECTION.get(args.get("name"))
            if info is None:
                return None, "__Type"
            return ("__Type", {"name": args.get("name"), "info": info}), "__Type"
        return _UNKNOWN, None
    if type_name == "Mutation":
        if field == "createIssue":
            inp = args.get("input") or {}
            found = _find_by_gid(store, inp.get("repositoryId"))
            if found is None or found[0] != "Repository":
                return ("CreateIssuePayload", {"issue": None}), "CreateIssuePayload"
            repo = found[1]
            issue = store.create_issue(repo, inp.get("title") or "", inp.get("body") or "", [])
            return ("CreateIssuePayload", {"issue": ("Issue", issue)}), "CreateIssuePayload"
        if field == "closeIssue":
            inp = args.get("input") or {}
            found = _find_by_gid(store, inp.get("issueId"))
            if found is None or found[0] != "Issue":
                return ("CloseIssuePayload", {"issue": None}), "CloseIssuePayload"
            _repo, issue = found[1]
            issue.state = "closed"
            return ("CloseIssuePayload", {"issue": ("Issue", issue)}), "CloseIssuePayload"
        if field == "createPullRequest":
            inp = args.get("input") or {}
            found = _find_by_gid(store, inp.get("repositoryId"))
            if found is None or found[0] != "Repository":
                return ("CreatePullRequestPayload", {"pullRequest": None}), "CreatePullRequestPayload"
            repo = found[1]
            head = inp.get("headRefName") or ""
            base = inp.get("baseRefName") or repo.default_branch
            head_sha = store.resolve_ref(repo, head)
            base_sha = store.resolve_ref(repo, base)
            if head_sha is None or base_sha is None:
                return ("CreatePullRequestPayload", {"pullRequest": None}), "CreatePullRequestPayload"
            files = restmod._diff_files(store, repo, base_sha, head_sha)
            pr = store.create_pull(repo, inp.get("title") or "", "", head, base,
                                   head_sha, base_sha,
                                   [(p, s, a, d) for p, s, a, d in files])
            return ("CreatePullRequestPayload", {"pullRequest": ("PullRequest", (repo, pr))}), "CreatePullRequestPayload"
        if field == "mergePullRequest":
            inp = args.get("input") or {}
            found = _find_by_gid(store, inp.get("pullRequestId"))
            if found is None or found[0] != "PullRequest":
                return ("MergePullRequestPayload", {"pullRequest": None}), "MergePullRequestPayload"
            repo, pr = found[1]
            if pr.state == "open":
                store.squash_merge(repo, pr)
            return ("MergePullRequestPayload", {"pullRequest": ("PullRequest", (repo, pr))}), "MergePullRequestPayload"
        if field == "addComment":
            inp = args.get("input") or {}
            found = _find_by_gid(store, inp.get("subjectId"))
            if found is None or found[0] != "Issue":
                return ("AddCommentPayload", {"commentEdge": None}), "AddCommentPayload"
            repo, issue = found[1]
            cid = store.add_comment(repo, issue, inp.get("body") or "")
            node = {"__type": "Comment", "id": "C_%d" % cid, "body": inp.get("body") or ""}
            return ("AddCommentPayload",
                    {"commentEdge": {"__type": "CommentEdge",
                                    "node": ("Comment", node)}}), "AddCommentPayload"
        return _UNKNOWN, None
    if type_name == "Repository":
        repo = obj
        if field == "id":
            return repo.gid, None
        if field == "databaseId":
            return repo.db_id, None
        if field == "name":
            return repo.name, None
        if field == "nameWithOwner":
            return "%s/%s" % (repo.owner, repo.name), None
        if field == "owner":
            return ("User", {"login": repo.owner}), "User"
        if field == "defaultBranchRef":
            return ("Ref", {"name": repo.default_branch}), "Ref"
        if field == "viewerPermission":
            return "ADMIN", None
        if field == "hasIssuesEnabled":
            return True, None
        if field == "description":
            return repo.description, None
        if field == "issue":
            issue = repo.issues.get(args.get("number"))
            if issue is None:
                return None, "Issue"
            return ("Issue", issue), "Issue"
        if field == "issues":
            return _resolve_issue_connection(ctx, repo, args), "IssueConnection"
        if field == "pullRequests":
            pulls = sorted(repo.pulls.values(), key=lambda p: p.seq)
            if args.get("first") is not None:
                pulls = pulls[:args["first"]]
            return {"__type": "PullRequestConnection",
                    "nodes": [("PullRequest", (repo, p)) for p in pulls]}, "PullRequestConnection"
        if field == "pullRequest":
            pr = repo.pulls.get(args.get("number"))
            if pr is None:
                return None, "PullRequest"
            return ("PullRequest", (repo, pr)), "PullRequest"
        return _UNKNOWN, None
    if type_name == "Issue":
        issue = obj
        if field == "id":
            return issue.gid, None
        if field == "number":
            return issue.number, None
        if field == "title":
            return issue.title, None
        if field == "state":
            return _issue_state(issue), None
        if field == "body":
            return issue.body, None
        return _UNKNOWN, None
    if type_name == "IssueConnection":
        if field == "nodes":
            return obj["nodes"], "Issue"
        if field == "pageInfo":
            return obj["pageInfo"], "PageInfo"
        return _UNKNOWN, None
    if type_name == "PageInfo":
        if field == "hasNextPage":
            return obj["hasNextPage"], None
        if field == "endCursor":
            return obj["endCursor"], None
        return _UNKNOWN, None
    if type_name == "PullRequest":
        repo, pr = obj
        if field == "id":
            return pr.gid, None
        if field == "number":
            return pr.number, None
        if field == "title":
            return pr.title, None
        if field == "body":
            return pr.body, None
        if field == "state":
            return _pr_state(pr), None
        if field == "merged":
            return pr.merged, None
        if field == "headRefName":
            return pr.head, None
        if field == "baseRefName":
            return pr.base, None
        if field == "mergeable":
            return "CONFLICTING" if store.merge_conflict(repo, pr) else "MERGEABLE", None
        if field == "files":
            nodes = [{"__type": "PullRequestChangedFile", "path": p}
                     for p, _s, _a, _d in pr.files]
            if args.get("first") is not None:
                nodes = nodes[:args["first"]]
            return {"__type": "PullRequestChangedFileConnection", "nodes": nodes}, "PullRequestChangedFileConnection"
        return _UNKNOWN, None
    if type_name == "PullRequestConnection":
        if field == "nodes":
            return obj["nodes"], "PullRequest"
        return _UNKNOWN, None
    if type_name == "PullRequestChangedFileConnection":
        if field == "nodes":
            return obj["nodes"], "PullRequestChangedFile"
        return _UNKNOWN, None
    if type_name == "PullRequestChangedFile":
        if field == "path":
            return obj["path"], None
        return _UNKNOWN, None
    if type_name == "User":
        if field == "login":
            return obj["login"], None
        return _UNKNOWN, None
    if type_name == "Ref":
        if field == "name":
            return obj["name"], None
        return _UNKNOWN, None
    if type_name == "__Type":
        info = obj["info"]
        if field == "name":
            return obj["name"], None
        if field == "fields":
            return [{"__type": "__Field", "name": n} for n in info["fields"]], "__Field"
        if field == "enumValues":
            if info["enum"] is None:
                return None, "__EnumValue"
            return [{"__type": "__EnumValue", "name": n} for n in info["enum"]], "__EnumValue"
        return _UNKNOWN, None
    if type_name in ("__Field", "__EnumValue"):
        if field == "name":
            return obj["name"], None
        return _UNKNOWN, None
    if type_name == "CreateIssuePayload":
        if field == "issue":
            return obj["issue"], "Issue"
        return _UNKNOWN, None
    if type_name == "CloseIssuePayload":
        if field == "issue":
            return obj["issue"], "Issue"
        return _UNKNOWN, None
    if type_name == "CreatePullRequestPayload":
        if field == "pullRequest":
            return obj["pullRequest"], "PullRequest"
        return _UNKNOWN, None
    if type_name == "MergePullRequestPayload":
        if field == "pullRequest":
            return obj["pullRequest"], "PullRequest"
        return _UNKNOWN, None
    if type_name == "AddCommentPayload":
        if field == "commentEdge":
            return obj["commentEdge"], "CommentEdge"
        return _UNKNOWN, None
    if type_name == "CommentEdge":
        if field == "node":
            return obj["node"], "Comment"
        return _UNKNOWN, None
    if type_name == "Comment":
        if field == "id":
            return obj["id"], None
        if field == "body":
            return obj["body"], None
        return _UNKNOWN, None
    return _UNKNOWN, None


def _run_operation(forge, op, variables):
    ctx = Ctx(forge, variables)
    try:
        if op["kind"] == "mutation":
            data = _execute(ctx, op["sel"], "Mutation", None)
        else:
            data = _execute(ctx, op["sel"], "Query", None)
    except Exception as exc:
        ctx.err(str(exc))
        data = None
    out = {"data": data}
    if ctx.errors:
        out["errors"] = ctx.errors
    return out


def handle(forge, handler):
    raw = handler._read_body() or b""
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        handler._send_json(200, {"data": None, "errors": [{"message": "Malformed JSON body"}]})
        return
    if isinstance(body, list):
        results = []
        for item in body:
            results.append(_run_one(forge, item))
        handler._send_json(200, results)
        return
    handler._send_json(200, _run_one(forge, body))


def _run_one(forge, item):
    if not isinstance(item, dict) or "query" not in item:
        return {"data": None, "errors": [{"message": "Missing query"}]}
    query = item.get("query") or ""
    variables = item.get("variables") or {}
    try:
        ops = _Parser(_tokenize(query)).parse_document()
    except Exception as exc:
        return {"data": None, "errors": [{"message": "Parse error: %s" % exc}]}
    op_name = item.get("operationName")
    op = None
    for o in ops:
        if op_name is None or o["name"] == op_name:
            op = o
            break
    if op is None:
        return {"data": None, "errors": [{"message": "Unknown operation"}]}
    return _run_operation(forge, op, variables)
