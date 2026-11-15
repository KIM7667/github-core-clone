"""Model Context Protocol endpoint at /mcp: JSON-RPC 2.0 over Streamable HTTP.

Speaks the modern revision 2026-07-28 via server/discover, and the legacy
handshake (initialize + notifications/initialized) for older revisions.
Tools act on the same store as REST.
"""
import json

from .rest import ApiError
from .store import BASE_ISO, VIEWER

SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "forge", "version": "1.0.0"}

# canonical name -> (aliases, required props, description, extra optional props)
TOOLS = [
    ("create_repo",
     ["create_repository", "repo_create", "repos_create", "repository_create"],
     ["name"],
     "Create a repository owned by the authenticated user.",
     {"name": {"type": "string", "description": "Repository name"},
      "description": {"type": "string", "description": "Repository description"},
      "private": {"type": "boolean", "description": "Whether the repository is private"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
    ("get_repo",
     ["get_repository", "repo_get", "repos_get", "repository_get"],
     ["owner", "repo"],
     "Get a repository by owner and name.",
     {"owner": {"type": "string", "description": "Repository owner"},
      "repo": {"type": "string", "description": "Repository name"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
    ("create_issue",
     ["issue_create", "issues_create"],
     ["owner", "repo", "title"],
     "Create an issue in a repository.",
     {"owner": {"type": "string", "description": "Repository owner"},
      "repo": {"type": "string", "description": "Repository name"},
      "title": {"type": "string", "description": "Issue title"},
      "body": {"type": "string", "description": "Issue body"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
    ("list_issues",
     ["issues_list", "issue_list"],
     ["owner", "repo"],
     "List issues in a repository.",
     {"owner": {"type": "string", "description": "Repository owner"},
      "repo": {"type": "string", "description": "Repository name"},
      "state": {"type": "string", "description": "Issue state: open, closed or all"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
    ("create_pull_request",
     ["pull_request_create", "pulls_create", "create_pr", "pr_create"],
     ["owner", "repo", "title", "head", "base"],
     "Create a pull request.",
     {"owner": {"type": "string", "description": "Repository owner"},
      "repo": {"type": "string", "description": "Repository name"},
      "title": {"type": "string", "description": "Pull request title"},
      "body": {"type": "string", "description": "Pull request body"},
      "head": {"type": "string", "description": "Head branch"},
      "base": {"type": "string", "description": "Base branch"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
    ("merge_pull_request",
     ["pull_request_merge", "pulls_merge", "merge_pr", "pr_merge"],
     ["owner", "repo", "pull_number"],
     "Merge a pull request.",
     {"owner": {"type": "string", "description": "Repository owner"},
      "repo": {"type": "string", "description": "Repository name"},
      "pull_number": {"type": "integer", "description": "Pull request number"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
    ("close_issue",
     ["issue_close", "issues_close"],
     ["owner", "repo", "issue_number"],
     "Close an issue.",
     {"owner": {"type": "string", "description": "Repository owner"},
      "repo": {"type": "string", "description": "Repository name"},
      "issue_number": {"type": "integer", "description": "Issue number"},
      "full_name": {"type": "string", "description": "owner/repo"}}),
]

# argument synonym sets (normalized property lookup, first hit wins)
SYNONYMS = {
    "owner": ["owner", "owner_login", "org", "organization", "organisation", "user",
              "user_login", "login", "account", "namespace", "owner_name"],
    "repo": ["repo", "repository", "repository_name", "repo_name", "repo_slug",
             "project", "project_name"],
    "full_name": ["full_name", "repo_full_name", "repository_full_name", "fullName"],
    "title": ["title", "issue_title", "pr_title", "pull_request_title", "subject"],
    "body": ["body", "issue_body", "description", "content", "text", "comment"],
    "head": ["head", "head_ref", "head_branch", "pull_request_head", "pr_head",
             "source", "source_ref", "source_branch", "from", "from_branch", "branch"],
    "base": ["base", "base_ref", "base_branch", "pull_request_base", "pr_base",
             "target", "target_ref", "target_branch", "into", "into_branch"],
    "pull_number": ["pull_number", "pull_request_number", "pr_number", "number",
                    "issue_number", "index"],
    "issue_number": ["issue_number", "number", "index", "pull_number"],
    "name": ["name", "repo", "repository", "repository_name", "repo_name"],
    "description": ["description", "about", "summary", "body"],
    "private": ["private", "is_private"],
    "state": ["state", "status"],
}


def _norm(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _find_tool(name):
    wanted = _norm(name)
    for canonical, aliases, _req, _desc, _props in TOOLS:
        if wanted == _norm(canonical):
            return canonical
        for alias in aliases:
            if wanted == _norm(alias):
                return canonical
        if wanted.endswith(_norm(canonical)):
            return canonical
    return None


def _tools_list():
    out = []
    for canonical, _aliases, required, desc, props in TOOLS:
        out.append({
            "name": canonical,
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })
    return out


TOOLS_LIST = _tools_list()
TOOLS_LIST_JSON = json.dumps({"tools": TOOLS_LIST}, separators=(",", ":"), sort_keys=True)


def _pick_arg(args, canonical):
    """Find the argument value for a canonical name via its synonym set."""
    if not isinstance(args, dict):
        return None
    normed = {}
    for k, v in args.items():
        normed[_norm(k)] = v
    for syn in SYNONYMS.get(canonical, [canonical]):
        key = _norm(syn)
        if key in normed:
            return normed[key]
    return None


def _owner_repo_from_args(store, args):
    owner = _pick_arg(args, "owner")
    repo_name = _pick_arg(args, "repo")
    full = _pick_arg(args, "full_name")
    if (owner is None or repo_name is None) and full and "/" in str(full):
        parts = str(full).split("/", 1)
        owner = owner or parts[0]
        repo_name = repo_name or parts[1]
    if owner is None and repo_name and "/" in str(repo_name):
        # "repository": "owner/repo" arrives as the repo synonym.
        parts = str(repo_name).split("/", 1)
        owner, repo_name = parts[0], parts[1]
    if owner is None or repo_name is None:
        return None, None, None
    if owner != VIEWER["login"]:
        return None, None, None
    repo = store.get_repo(owner, repo_name)
    return owner, repo_name, repo


class MCP:
    def __init__(self, store, token):
        self.store = store
        self.token = token

    def handle(self, method, headers, body):
        """Returns (status, headers, body_bytes)."""
        if method == "DELETE":
            return 405, {}, b'{"jsonrpc":"2.0","error":{"code":-32601,"message":"Method not found"},"id":null}'
        if method != "POST":
            return 405, {}, b'{"jsonrpc":"2.0","error":{"code":-32601,"message":"Method not found"},"id":null}'
        try:
            req = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            err = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}}
            return 400, {"Content-Type": "application/json"}, json.dumps(err).encode()
        if not isinstance(req, dict):
            err = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "Invalid Request"}}
            return 400, {"Content-Type": "application/json"}, json.dumps(err).encode()
        req_id = req.get("id")
        method_name = req.get("method")
        params = req.get("params") or {}
        if method_name is None:
            # A response to a server request we never sent; ignore.
            return 202, {}, b""
        # notifications (no id) get no result
        is_notification = req_id is None and method_name.startswith("notifications/")
        if is_notification:
            return 202, {}, b""
        if method_name == "server/discover":
            result = {"protocolVersion": "2026-07-28",
                      "serverInfo": dict(SERVER_INFO),
                      "capabilities": {"tools": {}},
                      "supportedVersions": list(SUPPORTED_VERSIONS)}
            return 200, {"Content-Type": "application/json"}, self._ok(req_id, result)
        if method_name == "initialize":
            offered = (params.get("protocolVersion") or "")
            negotiated = offered if offered in SUPPORTED_VERSIONS else "2026-07-28"
            result = {"protocolVersion": negotiated,
                      "capabilities": {"tools": {}},
                      "serverInfo": dict(SERVER_INFO)}
            return 200, {"Content-Type": "application/json"}, self._ok(req_id, result)
        if method_name == "tools/list":
            return 200, {"Content-Type": "application/json"}, self._ok(
                req_id, json.loads(TOOLS_LIST_JSON))
        if method_name == "tools/call":
            return self._tools_call(req_id, params, headers)
        if method_name == "ping":
            return 200, {"Content-Type": "application/json"}, self._ok(req_id, {})
        err = {"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32601, "message": "Method not found: %s" % method_name}}
        return 200, {"Content-Type": "application/json"}, json.dumps(err).encode()

    @staticmethod
    def _ok(req_id, result):
        return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode()

    @staticmethod
    def _err(req_id, code, message):
        return json.dumps({"jsonrpc": "2.0", "id": req_id,
                           "error": {"code": code, "message": message}}).encode()

    def _tools_call(self, req_id, params, headers):
        # Auth precedes tool dispatch.
        if self.token:
            auth = headers.get("authorization", "")
            if auth != "Bearer " + self.token:
                return 401, {"Content-Type": "application/json"}, json.dumps(
                    {"message": "Requires authentication"}).encode()
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        canonical = _find_tool(name)
        if canonical is None:
            return 200, {"Content-Type": "application/json"}, self._err(
                req_id, -32602, "Unknown tool: %s" % name)
        try:
            result = self._dispatch(canonical, args)
        except ApiError as e:
            result = {"isError": True,
                      "content": [{"type": "text", "text": e.message}]}
        except Exception as e:  # noqa: BLE001 - surface as a tool error, never HTTP 500
            result = {"isError": True,
                      "content": [{"type": "text", "text": "Tool error: %s" % e}]}
        return 200, {"Content-Type": "application/json"}, self._ok(req_id, result)

    def _missing(self, canonical, args):
        # A "owner/repo" value in any repo synonym satisfies owner + repo.
        owner, repo_name, _repo = _owner_repo_from_args(self.store, args)
        for _c, _a, required, _d, _p in TOOLS:
            if _c == canonical:
                for prop in required:
                    if prop == "owner" and owner is not None:
                        continue
                    if prop == "repo" and repo_name is not None:
                        continue
                    if _pick_arg(args, prop) is None:
                        return prop
        return None

    def _dispatch(self, canonical, args):
        missing = self._missing(canonical, args)
        if missing is not None:
            return {"isError": True,
                    "content": [{"type": "text",
                                 "text": "Missing required argument: %s" % missing}]}
        if canonical == "create_repo":
            return self._t_create_repo(args)
        if canonical == "get_repo":
            return self._t_get_repo(args)
        if canonical == "create_issue":
            return self._t_create_issue(args)
        if canonical == "list_issues":
            return self._t_list_issues(args)
        if canonical == "create_pull_request":
            return self._t_create_pr(args)
        if canonical == "merge_pull_request":
            return self._t_merge_pr(args)
        if canonical == "close_issue":
            return self._t_close_issue(args)
        raise ApiError(-32602, "Unknown tool")

    def _repo_or_error(self, args):
        _owner, _name, repo = _owner_repo_from_args(self.store, args)
        if repo is None:
            raise ApiError(404, "Repository not found")
        return repo

    def _t_create_repo(self, args):
        name = _pick_arg(args, "name")
        owner = _pick_arg(args, "owner") or VIEWER["login"]
        if owner != VIEWER["login"]:
            raise ApiError(404, "Repository not found")
        if self.store.get_repo(owner, name) is not None:
            raise ApiError(422, "Repository already exists")
        repo = self.store.create_repo(owner, name,
                                      description=_pick_arg(args, "description") or "",
                                      private=bool(_pick_arg(args, "private")))
        full = "%s/%s" % (owner, name)
        data = {"id": repo.id, "name": name, "full_name": full,
                "owner": owner, "private": repo.private,
                "description": repo.description, "default_branch": repo.default_branch}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}

    def _t_get_repo(self, args):
        repo = self._repo_or_error(args)
        full = "%s/%s" % (repo.owner, repo.name)
        data = {"id": repo.id, "name": repo.name, "full_name": full,
                "owner": repo.owner, "private": repo.private,
                "description": repo.description, "default_branch": repo.default_branch}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}

    def _t_create_issue(self, args):
        repo = self._repo_or_error(args)
        title = _pick_arg(args, "title")
        body = _pick_arg(args, "body") or ""
        repo.issue_seq += 1
        number = repo.issue_seq
        issue = {"id": self.store.next_id(), "number": number, "title": title,
                 "body": body, "state": "open", "labels": [], "comments": [],
                 "created_seq": self.store.next_seq()}
        repo.issues[number] = issue
        data = {"id": issue["id"], "number": number, "title": title, "body": body,
                "state": "open", "full_name": "%s/%s" % (repo.owner, repo.name)}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}

    def _t_list_issues(self, args):
        repo = self._repo_or_error(args)
        state = (_pick_arg(args, "state") or "open").lower()
        items = list(repo.issues.values())
        if state == "open":
            items = [i for i in items if i["state"] == "open"]
        elif state == "closed":
            items = [i for i in items if i["state"] == "closed"]
        items.sort(key=lambda i: i["created_seq"])
        data = {"issues": [{"id": i["id"], "number": i["number"], "title": i["title"],
                            "body": i["body"], "state": i["state"]} for i in items]}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}

    def _t_create_pr(self, args):
        repo = self._repo_or_error(args)
        title = _pick_arg(args, "title")
        body = _pick_arg(args, "body") or ""
        head = _pick_arg(args, "head")
        base = _pick_arg(args, "base") or repo.default_branch
        if ("refs/heads/" + head) not in repo.refs or ("refs/heads/" + base) not in repo.refs:
            raise ApiError(422, "head or base branch does not exist")
        for p in repo.pulls.values():
            if p["state"] == "open" and p["head"] == head and p["base"] == base:
                raise ApiError(422, "A pull request already exists for this head and base")
        repo.pull_seq += 1
        number = repo.pull_seq
        pr = {"id": self.store.next_id(), "number": number, "title": title,
              "body": body, "state": "open", "head": head, "base": base,
              "base_sha": repo.refs["refs/heads/" + base],
              "merged": False, "merged_at": None, "merge_commit_sha": None,
              "created_seq": self.store.next_seq()}
        repo.pulls[number] = pr
        data = {"id": pr["id"], "number": number, "title": title, "state": "open",
                "head": head, "base": base,
                "full_name": "%s/%s" % (repo.owner, repo.name)}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}

    def _t_merge_pr(self, args):
        repo = self._repo_or_error(args)
        number = _pick_arg(args, "pull_number")
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise ApiError(422, "pull_number must be an integer")
        pr = repo.pulls.get(number)
        if pr is None:
            raise ApiError(404, "Pull request not found")
        if pr["state"] != "open":
            raise ApiError(405, "Pull request is already closed")
        base_tip = repo.refs.get("refs/heads/" + pr["base"])
        head_tip = repo.refs.get("refs/heads/" + pr["head"])
        if base_tip is None or head_tip is None:
            raise ApiError(405, "Cannot merge: missing branch")
        from . import vcs
        if vcs.pr_conflicts(repo.objects, pr, base_tip, head_tip):
            raise ApiError(405, "Merge conflict")
        head_tree = vcs.commit_tree_at(repo.objects, head_tip)
        message = "Merge pull request #%d from %s/%s" % (number, repo.owner, pr["head"])
        merge_sha = vcs.make_commit(repo.objects, head_tree, [base_tip], message)
        repo.refs["refs/heads/" + pr["base"]] = merge_sha
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merged_at"] = BASE_ISO
        pr["merge_commit_sha"] = merge_sha
        data = {"number": number, "merged": True, "sha": merge_sha, "state": "closed"}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}

    def _t_close_issue(self, args):
        repo = self._repo_or_error(args)
        number = _pick_arg(args, "issue_number")
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise ApiError(422, "issue_number must be an integer")
        issue = repo.issues.get(number)
        if issue is None:
            raise ApiError(404, "Issue not found")
        issue["state"] = "closed"
        issue.pop("_json", None)
        data = {"id": issue["id"], "number": number, "title": issue["title"],
                "state": "closed"}
        return {"content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data}
