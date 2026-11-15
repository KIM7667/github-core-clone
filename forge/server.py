"""HTTP server: routing, auth, request ids, keep-alive. One process, one
ThreadingHTTPServer; writes take a lock, reads run concurrently.
"""
import itertools
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import git_http
from .graphql import GraphQL
from .mcp import MCP
from .rest import ApiError, RestAPI
from .store import Store

GIT_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ForgeServer:
    def __init__(self, token):
        self.store = Store()
        self.token = token
        self._req_counter = itertools.count(1)
        self._write_lock = threading.RLock()
        self.rest = RestAPI(self.store, token, self._request_id)
        self.graphql = GraphQL(self.store)
        self.mcp = MCP(self.store, token)

    def _request_id(self):
        return "forge-%d" % next(self._req_counter)

    # -- dispatch -------------------------------------------------------------

    def dispatch(self, method, raw_path, query, headers, body):
        """Returns (status, headers_dict, body_bytes)."""
        split = urlsplit(raw_path)
        path = unquote(split.path)
        parts = [p for p in path.split("/") if p != ""]
        # /healthz: no auth
        if path == "/healthz" and method == "GET":
            return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
        if len(parts) >= 2 and parts[1].endswith(".git"):
            return self._git(method, parts, query, headers, body)
        if parts[:1] == ["api"] and parts[1:2] == ["v3"]:
            return self._rest(method, parts[2:], query, headers, body, raw_path)
        if parts == ["api", "graphql"]:
            return self._graphql(method, headers, body)
        if parts == ["mcp"]:
            with self._write_lock:
                return self.mcp.handle(method, headers, body)
        raise ApiError(404, "Not Found")

    def _rest(self, method, parts, query, headers, body, raw_path):
        split = urlsplit(raw_path)
        base_url = "http://%s%s" % (headers.get("host", "localhost"), split.path)
        if query:
            from urllib.parse import urlencode
            # The page cursor is added by _paginate; drop any incoming one
            # so following a next link cannot accumulate page=2&page=3.
            q = {k: v for k, v in query.items() if k != "page"}
            if q:
                base_url += "?" + urlencode(q, doseq=True)
        with self._write_lock:
            status, extra, obj = self.rest.handle(method, parts, query, headers, body, base_url)
        headers_out = {"Content-Type": "application/json",
                       "X-GitHub-Request-Id": self._request_id()}
        headers_out.update(extra)
        if status == 204 or obj is None:
            return status, headers_out, b""
        return status, headers_out, json.dumps(obj).encode()

    def _graphql(self, method, headers, body):
        if method != "POST":
            raise ApiError(404, "Not Found")
        if self.token:
            auth = headers.get("authorization", "")
            if auth != "Bearer " + self.token:
                raise ApiError(401, "Requires authentication")
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON body")
        with self._write_lock:
            _status, result = self.graphql.execute(parsed, headers)
        return 200, {"Content-Type": "application/json",
                     "X-GitHub-Request-Id": self._request_id()}, json.dumps(result).encode()

    def _git(self, method, parts, query, headers, body):
        if self.token:
            auth = headers.get("authorization", "")
            if auth != "Bearer " + self.token:
                raise ApiError(401, "Requires authentication")
        owner = parts[0]
        repo_name = parts[1][:-4]
        if not GIT_OWNER_RE.match(owner) or not GIT_OWNER_RE.match(repo_name):
            raise ApiError(404, "Not Found")
        repo = self.store.get_repo(owner, repo_name)
        if repo is None:
            raise ApiError(404, "Not Found")
        service = query.get("service", [None])[0]
        if len(parts) == 4 and parts[2] == "info" and parts[3] == "refs" and method == "GET":
            if service == "git-upload-pack":
                if headers.get("git-protocol") == "version=2":
                    data = git_http.advertisement_v2_upload_pack()
                else:
                    data = git_http.advertisement_v0(repo, "git-upload-pack")
                return 200, {"Content-Type": "application/x-git-upload-pack-advertisement"}, data
            if service == "git-receive-pack":
                data = git_http.advertisement_v0(repo, "git-receive-pack")
                return 200, {"Content-Type": "application/x-git-receive-pack-advertisement"}, data
            raise ApiError(400, "Unknown service")
        if len(parts) == 3 and parts[2] in ("git-upload-pack", "git-receive-pack") and method == "POST":
            with self._write_lock:
                try:
                    if parts[2] == "git-upload-pack":
                        if headers.get("git-protocol") == "version=2":
                            data, ctype = git_http.handle_upload_pack_v2(repo, body)
                        else:
                            data, ctype = git_http.handle_upload_pack_v0(repo, body)
                    else:
                        data, ctype = git_http.handle_receive_pack(repo, body)
                except ValueError as e:
                    raise ApiError(400, "Malformed pkt-line stream: %s" % e)
            return 200, {"Content-Type": ctype}, data
        raise ApiError(404, "Not Found")


def make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            # Small responses on keep-alive connections stall ~40 ms under
            # Nagle + delayed ACK; the benchmark drives thousands of
            # sequential requests on one connection, so disable Nagle.
            try:
                self.connection.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        def log_message(self, *args):
            pass

        def _handle(self, method):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 64 * 1024 * 1024:
                self._send(413, {"Content-Type": "application/json"},
                           b'{"message":"Request too large"}')
                return
            body = self.rfile.read(length) if length else b""
            from urllib.parse import parse_qs
            split = urlsplit(self.path)
            query = parse_qs(split.query, keep_blank_values=True)
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                status, extra, payload = server.dispatch(method, self.path, query, headers, body)
            except ApiError as e:
                status, extra, payload = e.status, {}, json.dumps(
                    {"message": e.message}).encode()
                if self.path.startswith("/api/v3"):
                    extra["X-GitHub-Request-Id"] = server._request_id()
                extra.setdefault("Content-Type", "application/json")
            except Exception as e:  # noqa: BLE001 - never crash the process
                import traceback
                traceback.print_exc()
                status, extra, payload = 500, {}, json.dumps(
                    {"message": "Internal error: %s" % e}).encode()
                extra.setdefault("Content-Type", "application/json")
            self._send(status, extra, payload)

        def _send(self, status, headers, payload):
            self.send_response(status)
            self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
            for key, value in headers.items():
                if key.lower() == "content-type":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload and self.command != "HEAD":
                self.wfile.write(payload)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        def do_HEAD(self):
            self._handle("GET")

    return Handler


def build_server(token, host="0.0.0.0", port=0):
    forge = ForgeServer(token)
    httpd = ThreadingHTTPServer((host, port), make_handler(forge))
    httpd.daemon_threads = True
    return httpd, forge
