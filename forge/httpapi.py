"""HTTP layer: threaded server, routing, auth, JSON envelope.

Routes:
  GET  /healthz                 -> {"ok": true}, no auth
  /api/v3/...                   -> REST (forge.rest)
  POST /api/graphql            -> GraphQL (forge.graphql)
  /mcp                         -> MCP (forge.mcp)
  /<owner>/<repo>.git/...      -> git smart HTTP (forge.gitproto)
Anything else: 404 JSON.
"""

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gitproto, graphql, mcp, rest, store

MAX_BODY = 64 * 1024 * 1024


class Forge:
    def __init__(self, token):
        self.store = store.Store()
        self.token = token or ""

    def check_auth(self, headers):
        """True when the request carries the trial bearer token."""
        if not self.token:
            # No token configured (local dev): any non-empty bearer passes.
            auth = headers.get("Authorization") or ""
            return auth.startswith("Bearer ") and len(auth) > 7
        auth = headers.get("Authorization") or ""
        return auth == "Bearer " + self.token


def make_handler(forge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Forge/1.0"

        def setup(self):
            # Disable Nagle on the accepted socket: without TCP_NODELAY,
            # small request/response round trips on localhost stall on the
            # Nagle + delayed-ACK interaction (~40 ms per request), which
            # alone blows the 2000-op read workload's 60 s budget.
            try:
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            super().setup()

        def log_message(self, *args):
            pass

        # -- helpers -------------------------------------------------
        def _send_json(self, status, obj, extra_headers=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-GitHub-Request-Id", forge.store.next_req_id())
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status, body, content_type, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("X-GitHub-Request-Id", forge.store.next_req_id())
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, status, message):
            self._send_json(status, {"message": message})

        def _read_body(self):
            # Cached: the body is read once per request, up front, so a
            # handler that returns early can never leave bytes in the
            # keep-alive stream and desync the next request.
            if hasattr(self, "_body_cache"):
                return self._body_cache
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._body_cache = None
                return None
            self._body_cache = self.rfile.read(n) if n else b""
            return self._body_cache

        def _consume_body(self):
            self._read_body()

        def _unauthorized(self):
            self._err(401, "Requires authentication")

        # -- routing -------------------------------------------------
        def _route(self):
            # One handler instance serves a whole keep-alive connection,
            # so drop the previous request's cached body first.
            self.__dict__.pop("_body_cache", None)
            self._consume_body()
            path = self.path.split("?", 1)[0]
            if path == "/healthz" and self.command == "GET":
                self._send_json(200, {"ok": True})
                return
            if path.startswith("/api/v3"):
                if not forge.check_auth(self.headers):
                    self._unauthorized()
                    return
                rest.handle(forge, self, path)
                return
            if path == "/api/graphql":
                if self.command != "POST":
                    self._err(404, "Not Found")
                    return
                if not forge.check_auth(self.headers):
                    self._send_json(401, {"message": "Requires authentication"})
                    return
                graphql.handle(forge, self)
                return
            if path == "/mcp":
                mcp.handle(forge, self, path)
                return
            if ".git/" in path or path.endswith(".git"):
                if not forge.check_auth(self.headers):
                    self._unauthorized()
                    return
                gitproto.handle(forge, self, path)
                return
            self._err(404, "Not Found")

        def do_GET(self):
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # never leak a stack as HTML
                try:
                    self._err(500, "Internal Server Error: %s" % exc)
                except Exception:
                    pass

        do_POST = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET

    return Handler


def serve(forge, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(forge))
    httpd.daemon_threads = True
    return httpd


def token_from_env():
    return os.environ.get("MAKEFUN_SHOP_TOKEN", "")
