"""Entry point: python3 main.py

Lifecycle (trial mode, MAKEFUN_ENV_URL set):
  1. Bind an ephemeral port (never 8080).
  2. POST {"port": N} to $MAKEFUN_ENV_URL/register.
  3. Poll GET $MAKEFUN_ENV_URL/done until {"done": true}.
  4. Exit 0.

Local mode (MAKEFUN_ENV_URL unset): serve on FORGE_PORT (default 18080)
until killed, for the documented local runner (sdk/run.ts).
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.httpapi import Forge, serve, token_from_env


def _http_json(method, url, payload=None, timeout=5):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def main():
    forge = Forge(token_from_env())
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if env_url:
        httpd = serve(forge, "0.0.0.0", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        # Register (first register wins; retry briefly while the env comes up).
        registered = False
        for _ in range(50):
            status, _body = _http_json("POST", env_url + "/register", {"port": port})
            if status in (200, 201, 204, 409):
                registered = True
                break
            time.sleep(0.2)
        if not registered:
            httpd.shutdown()
            return 1
        # Poll /done until the suite finishes.
        def _stop(signum, frame):
            httpd.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        while True:
            _status, body = _http_json("GET", env_url + "/done")
            if isinstance(body, dict) and body.get("done") is True:
                break
            time.sleep(0.25)
        httpd.shutdown()
        return 0
    # Local mode: fixed port, run until killed.
    port = int(os.environ.get("FORGE_PORT", "18080"))
    httpd = serve(forge, "127.0.0.1", port)
    sys.stderr.write("forge listening on 127.0.0.1:%d\n" % port)
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
