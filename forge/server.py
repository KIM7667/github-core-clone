"""HTTP server: routing, auth, request ids, keep-alive. One process, one
ThreadingHTTPServer; writes take a lock, reads run concurrently.
"""
import itertools
import json
import re
import socket
import threading
from http.server import ThreadingHTTPServer
from socketserver import BaseRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit

from . import git_http
from .graphql import GraphQL
from .mcp import MCP
from .rest import ApiError, RestAPI
from .store import Store

GIT_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class _RepoSnapshot:
    """Shallow copies of a repo's refs and object table, taken under the
    write lock. Object payloads are immutable bytes, so readers can walk
    the snapshot lock-free while pushes keep mutating the live repo."""

    __slots__ = ("refs", "objects", "default_branch")

    def __init__(self, repo):
        self.refs = dict(repo.refs)
        self.objects = dict(repo.objects)
        self.default_branch = repo.default_branch


class ForgeServer:
    def __init__(self, token):
        self.store = Store()
        self.token = token
        self._auth = "Bearer " + token if token else None
        self._req_counter = itertools.count(1)
        self._write_lock = threading.RLock()
        self.rest = RestAPI(self.store, token, self._request_id)
        self.graphql = GraphQL(self.store)
        self.mcp = MCP(self.store, token)

    def _request_id(self):
        return "forge-%d" % next(self._req_counter)

    # -- dispatch -------------------------------------------------------------

    def dispatch(self, method, split, query, headers, body):
        """Returns (status, headers_dict, body_bytes)."""
        path = unquote(split.path)
        parts = [p for p in path.split("/") if p != ""]
        # /healthz: no auth
        if path == "/healthz" and method == "GET":
            return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
        if len(parts) >= 2 and parts[1].endswith(".git"):
            return self._git(method, parts, query, headers, body)
        if parts[:1] == ["api"] and parts[1:2] == ["v3"]:
            return self._rest(method, parts[2:], query, headers, body, split)
        if parts == ["api", "graphql"]:
            return self._graphql(method, headers, body)
        if parts == ["mcp"]:
            with self._write_lock:
                return self.mcp.handle(method, headers, body)
        raise ApiError(404, "Not Found")

    def _rest(self, method, parts, query, headers, body, split):
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
        if isinstance(obj, bytes):
            return status, headers_out, bytes(obj)
        return status, headers_out, json.dumps(obj, separators=(",", ":")).encode()

    def _graphql(self, method, headers, body):
        if method != "POST":
            raise ApiError(404, "Not Found")
        if self._auth is not None:
            if headers.get("authorization", "") != self._auth:
                raise ApiError(401, "Requires authentication")
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON body")
        with self._write_lock:
            _status, result = self.graphql.execute(parsed, headers)
        return 200, {"Content-Type": "application/json",
                     "X-GitHub-Request-Id": self._request_id()}, json.dumps(
                         result, separators=(",", ":")).encode()

    def _git(self, method, parts, query, headers, body):
        if self._auth is not None:
            if headers.get("authorization", "") != self._auth:
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
                    data = git_http.advertisement_v0(self._snapshot(repo), "git-upload-pack")
                return 200, {"Content-Type": "application/x-git-upload-pack-advertisement"}, data
            if service == "git-receive-pack":
                data = git_http.advertisement_v0(self._snapshot(repo), "git-receive-pack")
                return 200, {"Content-Type": "application/x-git-receive-pack-advertisement"}, data
            raise ApiError(400, "Unknown service")
        if len(parts) == 3 and parts[2] in ("git-upload-pack", "git-receive-pack") and method == "POST":
            try:
                if parts[2] == "git-upload-pack":
                    # Read-only: run on a consistent snapshot without the
                    # write lock, so concurrent clones overlap their waits
                    # instead of queueing behind each other's fetches.
                    snap = self._snapshot(repo)
                    if headers.get("git-protocol") == "version=2":
                        data, ctype = git_http.handle_upload_pack_v2(snap, body)
                    else:
                        data, ctype = git_http.handle_upload_pack_v0(snap, body)
                else:
                    with self._write_lock:
                        data, ctype = git_http.handle_receive_pack(repo, body)
            except ValueError as e:
                raise ApiError(400, "Malformed pkt-line stream: %s" % e)
            return 200, {"Content-Type": ctype}, data
        raise ApiError(404, "Not Found")

    def _snapshot(self, repo):
        """A consistent (refs, objects) pair for read-only git work."""
        with self._write_lock:
            return _RepoSnapshot(repo)


_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently",
    302: "Found", 304: "Not Modified", 400: "Bad Request",
    401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 413: "Content Too Large",
    422: "Unprocessable Entity", 500: "Internal Server Error",
}

_MAX_BODY = 64 * 1024 * 1024
_MAX_HEAD = 1024 * 1024


class _Conn:
    """Buffered reader over one socket: recv chunks land in a bytearray
    and are consumed in place, so a keep-alive connection costs one
    syscall per request in the common case."""

    __slots__ = ("sock", "buf")

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("peer closed")
        self.buf += chunk

    def read_head(self):
        """Return the header block (without the CRLFCRLF terminator)."""
        while True:
            idx = self.buf.find(b"\r\n\r\n")
            if idx >= 0:
                head = bytes(self.buf[:idx])
                del self.buf[:idx + 4]
                return head
            if len(self.buf) > _MAX_HEAD:
                raise ValueError("header block too large")
            self._fill()

    def read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_line(self):
        while True:
            idx = self.buf.find(b"\r\n")
            if idx >= 0:
                line = bytes(self.buf[:idx])
                del self.buf[:idx + 2]
                return line
            self._fill()


class _Target:
    """Minimal urlsplit stand-in for origin-form targets (the only form
    these HTTP clients send): just .path and .query, without urlsplit's
    validation passes on every request."""

    __slots__ = ("path", "query")

    def __init__(self, path, query):
        self.path = path
        self.query = query


def _split_target(target):
    if target.startswith("/"):
        path, _q, query = target.partition("?")
        return _Target(path, query)
    split = urlsplit(target)
    return _Target(split.path, split.query)


def _read_chunked(conn):
    body = bytearray()
    while True:
        line = conn.read_line()
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise ValueError("bad chunk size")
        if size == 0:
            # trailer section ends with an empty line
            while True:
                if conn.read_line() == b"":
                    break
            return bytes(body)
        body += conn.read_exact(size)
        conn.read_exact(2)  # trailing CRLF


def make_handler(server):
    class Handler(BaseRequestHandler):
        def handle(self):
            # Small responses on keep-alive connections stall ~40 ms
            # under Nagle + delayed ACK; the benchmark drives thousands
            # of sequential requests on one connection, so disable Nagle.
            try:
                self.request.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            conn = _Conn(self.request)
            try:
                while self._one_request(conn):
                    pass
            except (ConnectionError, OSError):
                pass

        def _one_request(self, conn):
            """Serve one request; return False when the connection ends."""
            try:
                head = conn.read_head()
            except ConnectionError:
                return False
            except ValueError:
                self._reply(conn, 400, {}, b'{"message":"Bad Request"}', True)
                return False
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            if len(parts) != 3:
                self._reply(conn, 400, {}, b'{"message":"Bad Request"}', True)
                return False
            method = parts[0].decode("latin-1")
            target = parts[1].decode("latin-1")
            version = parts[2].decode("latin-1")
            headers = {}
            for line in lines[1:]:
                key, sep, value = line.partition(b":")
                if not sep:
                    continue
                headers[key.decode("latin-1").strip().lower()] = \
                    value.decode("latin-1").strip()
            connection = headers.get("connection", "").lower()
            if version == "HTTP/1.1":
                keep_alive = connection != "close"
            elif version == "HTTP/1.0":
                keep_alive = connection == "keep-alive"
            else:
                keep_alive = False
            # ---- body
            body = b""
            try:
                if "chunked" in headers.get("transfer-encoding", "").lower():
                    body = _read_chunked(conn)
                else:
                    try:
                        length = int(headers.get("content-length") or 0)
                    except ValueError:
                        length = 0
                    if length > _MAX_BODY:
                        self._reply(conn, 413, {"Content-Type": "application/json"},
                                    b'{"message":"Request too large"}', True)
                        return False
                    if length:
                        if headers.get("expect", "").lower() == "100-continue":
                            self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                        body = conn.read_exact(length)
            except (ConnectionError, OSError):
                return False
            except ValueError:
                self._reply(conn, 400, {}, b'{"message":"Bad Request"}', True)
                return False
            # ---- dispatch
            split = _split_target(target)
            query = parse_qs(split.query, keep_blank_values=True) \
                if split.query else {}
            try:
                status, extra, payload = server.dispatch(
                    "GET" if method == "HEAD" else method,
                    split, query, headers, body)
            except ApiError as e:
                status, extra, payload = e.status, {}, json.dumps(
                    {"message": e.message}, separators=(",", ":")).encode()
                if split.path.startswith("/api/v3"):
                    extra["X-GitHub-Request-Id"] = server._request_id()
                extra.setdefault("Content-Type", "application/json")
            except Exception as e:  # noqa: BLE001 - never crash the process
                import traceback
                traceback.print_exc()
                status, extra, payload = 500, {}, json.dumps(
                    {"message": "Internal error: %s" % e},
                    separators=(",", ":")).encode()
                extra.setdefault("Content-Type", "application/json")
            body_len = len(payload)
            if method == "HEAD":
                payload = b""
            self._reply(conn, status, extra, payload, not keep_alive, body_len)
            return keep_alive

        def _reply(self, conn, status, headers, payload, close, body_len=None):
            out = bytearray()
            out += ("HTTP/1.1 %d %s\r\n" % (
                status, _REASONS.get(status, ""))).encode("latin-1")
            out += ("Content-Type: %s\r\n" % headers.get(
                "Content-Type", "application/json")).encode("latin-1")
            for key, value in headers.items():
                if key.lower() == "content-type":
                    continue
                out += ("%s: %s\r\n" % (key, value)).encode("latin-1")
            out += ("Content-Length: %d\r\n" % (
                len(payload) if body_len is None else body_len)).encode("latin-1")
            if close:
                out += b"Connection: close\r\n"
            out += b"\r\n"
            out += payload
            conn.sock.sendall(bytes(out))

    return Handler


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # The clone workload opens dozens of connections at once; the
    # default backlog of 5 drops SYNs and costs seconds in retries.
    request_queue_size = 128


def build_server(token, host="0.0.0.0", port=0):
    # Concurrent clone bursts run dozens of handler threads; a longer
    # switch interval means fewer GIL hand-offs mid-request.
    import sys
    sys.setswitchinterval(0.05)
    forge = ForgeServer(token)
    httpd = _HTTPServer((host, port), make_handler(forge))
    return httpd, forge
