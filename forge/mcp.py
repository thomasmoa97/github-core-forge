"""MCP over Streamable HTTP (legacy era: initialize + notifications).

The suite probes server/discover first; we answer -32601 so it falls back
to the legacy handshake (2025-11-25). tools/list is byte-identical across
calls (built once). tools/call acts on the same store as REST.
"""

import json

from . import rest as restmod
from .store import VIEWER_LOGIN

SESSION_ID = "forge-session-1"
PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "private": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "head": {"type": "string"},
                "base": {"type": "string"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request with a squash commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pull_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

_TOOLS_JSON = json.dumps({"tools": TOOLS}, sort_keys=True)


def _result_text(obj):
    return json.dumps(obj, sort_keys=True)


def _tool_result(obj):
    text = _result_text(obj)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": obj,
            "isError": False}


def _tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _split_full_name(args):
    """Accept owner/repo directly, or a full_name 'owner/repo'."""
    owner = args.get("owner") or args.get("owner_login") or args.get("login")
    repo = args.get("repo") or args.get("repository") or args.get("repository_name")
    full = args.get("full_name") or args.get("repo_full_name")
    if full and "/" in str(full):
        owner, repo = str(full).split("/", 1)
    return owner, repo


def _call_tool(forge, name, args):
    store = forge.store
    args = args or {}
    lname = str(name or "").lower().replace("_", "").replace("-", "")
    if lname == "createrepo":
        repo_name = args.get("name") or args.get("repo") or args.get("repository")
        if not repo_name:
            return _tool_error("Missing required argument: name")
        owner = args.get("owner") or VIEWER_LOGIN
        if store.get_repo(owner, repo_name):
            return _tool_error("Repository already exists")
        repo = store.create_repo(owner, repo_name, args.get("description") or "",
                                 bool(args.get("private", False)))
        return _tool_result(restmod._repo_json(repo))
    if lname == "getrepo":
        owner, repo_name = _split_full_name(args)
        if not owner or not repo_name:
            return _tool_error("Missing required argument: owner/repo")
        repo = store.get_repo(owner, repo_name)
        if repo is None:
            return _tool_error("Not Found")
        return _tool_result(restmod._repo_json(repo))
    if lname == "createissue":
        owner, repo_name = _split_full_name(args)
        title = args.get("title")
        if not owner or not repo_name or not title:
            return _tool_error("Missing required argument: owner, repo, title")
        repo = store.get_repo(owner, repo_name)
        if repo is None:
            return _tool_error("Not Found")
        issue = store.create_issue(repo, title, args.get("body") or "", [])
        return _tool_result(restmod._issue_json(issue))
    if lname == "listissues":
        owner, repo_name = _split_full_name(args)
        if not owner or not repo_name:
            return _tool_error("Missing required argument: owner/repo")
        repo = store.get_repo(owner, repo_name)
        if repo is None:
            return _tool_error("Not Found")
        state = args.get("state") or "open"
        issues = [i for i in repo.issues.values() if state == "all" or i.state == state]
        issues.sort(key=lambda i: i.seq)
        arr = [restmod._issue_json(i) for i in issues]
        return {"content": [{"type": "text", "text": json.dumps(arr, sort_keys=True)}],
                "structuredContent": {"issues": arr},
                "isError": False}
    if lname == "createpullrequest":
        owner, repo_name = _split_full_name(args)
        title = args.get("title")
        head = args.get("head") or args.get("head_ref") or args.get("head_branch")
        base = args.get("base") or args.get("base_ref") or args.get("base_branch")
        if not owner or not repo_name or not title or not head or not base:
            return _tool_error("Missing required argument: owner, repo, title, head, base")
        repo = store.get_repo(owner, repo_name)
        if repo is None:
            return _tool_error("Not Found")
        head_sha = store.resolve_ref(repo, head)
        base_sha = store.resolve_ref(repo, base)
        if head_sha is None or base_sha is None:
            return _tool_error("head or base branch not found")
        files = restmod._diff_files(store, repo, base_sha, head_sha)
        pr = store.create_pull(repo, title, args.get("body") or "", head, base,
                               head_sha, base_sha,
                               [(p, s, a, d) for p, s, a, d in files])
        return _tool_result(restmod._pull_json(pr))
    if lname == "mergepullrequest":
        owner, repo_name = _split_full_name(args)
        number = args.get("pull_number")
        if number is None:
            number = args.get("number") or args.get("pr_number")
        if not owner or not repo_name or number is None:
            return _tool_error("Missing required argument: owner, repo, pull_number")
        repo = store.get_repo(owner, repo_name)
        if repo is None:
            return _tool_error("Not Found")
        try:
            number = int(number)
        except (TypeError, ValueError):
            return _tool_error("pull_number must be an integer")
        pr = repo.pulls.get(number)
        if pr is None:
            return _tool_error("Not Found")
        if pr.state != "open":
            return _tool_error("Pull request is already closed")
        merge_sha = store.squash_merge(repo, pr)
        if merge_sha is None:
            return _tool_error("Merge conflict")
        return _tool_result({"merged": True, "sha": merge_sha, "number": number})
    return None


def handle(forge, handler, path):
    if handler.command == "DELETE":
        handler._send_bytes(204, b"", "application/json")
        return
    if handler.command != "POST":
        handler._send_json(404, {"message": "Not Found"})
        return
    # Auth: every /mcp request needs the bearer token.
    if not forge.check_auth(handler.headers):
        handler._send_json(401, {"message": "Requires authentication"})
        return
    raw = handler._read_body()
    if raw is None:
        raw = b""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        payload = json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Parse error"}}).encode("utf-8")
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return
    if body is None:
        _rpc_error(handler, None, -32700, "Parse error")
        return
    # Notification (no id): acknowledge with 202, no body.
    if isinstance(body, dict) and "id" not in body:
        handler._send_bytes(202, b"", "application/json")
        return
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if method == "server/discover":
        _rpc_error(handler, req_id, -32601, "Method not found")
        return
    if method == "initialize":
        offered = params.get("protocolVersion") or PROTOCOL_VERSION
        result = {
            "protocolVersion": offered,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "forge", "version": "1.0.0"},
        }
        _rpc_result(handler, req_id, result, session=True)
        return
    if method == "tools/list":
        # Byte-identical across calls: send the prebuilt payload.
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Mcp-Session-Id", SESSION_ID)
        payload = ('{"jsonrpc": "2.0", "id": %s, "result": %s}'
                   % (json.dumps(req_id), _TOOLS_JSON)).encode("utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        res = _call_tool(forge, name, args)
        if res is None:
            _rpc_error(handler, req_id, -32601, "Method not found")
            return
        _rpc_result(handler, req_id, res)
        return
    _rpc_error(handler, req_id, -32601, "Method not found")


def _rpc_result(handler, req_id, result, session=False):
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    if session:
        handler.send_header("Mcp-Session-Id", SESSION_ID)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _rpc_error(handler, req_id, code, message):
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id,
                          "error": {"code": code, "message": message}}).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
