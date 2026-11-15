"""github-core-clone-01 entry: a standalone GitHub-compatible git forge.

One process, pure stdlib. Lifecycle (the kit's prompt.txt):
  1. Bind an ephemeral port (never 8080).
  2. POST {"port": N} to $MAKEFUN_ENV_URL/register.
  3. Poll GET $MAKEFUN_ENV_URL/done until {"done": true}.
  4. Exit 0.

Without MAKEFUN_ENV_URL (local dev), it prints its port and serves forever.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.server import build_server  # noqa: E402


def _http_json(url, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, {}


def main():
    token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    httpd, _forge = build_server(token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    if not env_url:
        print("forge listening on port %d (no MAKEFUN_ENV_URL; dev mode)" % port, flush=True)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        return 0
    # Register with retries inside the boot budget.
    deadline = time.monotonic() + 18
    registered = False
    delay = 0.25
    while time.monotonic() < deadline:
        status, _body = _http_json(env_url + "/register", {"port": port}, timeout=5)
        if status in (200, 409):
            registered = True
            break
        time.sleep(delay)
        delay = min(delay * 2, 2.0)
    if not registered:
        print("forge: register failed; exiting", file=sys.stderr, flush=True)
        httpd.shutdown()
        return 1
    # Poll /done until the suite finishes.
    delay = 0.25
    while True:
        status, body = _http_json(env_url + "/done", None, timeout=30)
        if status == 200 and body.get("done") is True:
            break
        time.sleep(delay)
        delay = min(delay * 2, 2.0)
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
