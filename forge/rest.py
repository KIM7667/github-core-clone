"""GitHub REST v3 API under /api/v3: repositories, branches, commits,
contents, labels, issues, comments and pull requests. Pure stdlib.
"""
import base64
import binascii
import json

from . import gitobj, vcs
from .gitobj import OBJ_BLOB, OBJ_COMMIT, ZERO
from .store import BASE_ISO, VIEWER

MAX_BODY = 8 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _b64decode(s):
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        raise ApiError(400, "Invalid base64 content")


class RestAPI:
    def __init__(self, store, token, request_id):
        self.store = store
        self.token = token
        self.request_id = request_id  # callable returning a new id string

    # ---------------------------------------------------------------- plumbing

    def _check_auth(self, headers):
        if not self.token:
            return
        auth = headers.get("authorization", "")
        if auth != "Bearer " + self.token:
            raise ApiError(401, "Requires authentication")

    def _owner_repo(self, owner, name):
        if owner != VIEWER["login"]:
            raise ApiError(404, "Not Found")
        repo = self.store.get_repo(owner, name)
        if repo is None:
            raise ApiError(404, "Not Found")
        return repo

    def _parse_body(self, body):
        if body is None or body == b"":
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON body")
        if not isinstance(data, dict):
            raise ApiError(400, "Invalid JSON body")
        return data

    def _page_params(self, query):
        try:
            per_page = int(query.get("per_page", ["30"])[0])
        except ValueError:
            per_page = 30
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            page = 1
        per_page = max(1, min(100, per_page))
        page = max(1, page)
        return per_page, page

    def _paginate(self, items, query, base_url):
        per_page, page = self._page_params(query)
        total = len(items)
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        headers = {}
        if base_url and start + per_page < total:
            sep = "&" if "?" in base_url else "?"
            nxt = "%s%spage=%d" % (base_url, sep, page + 1)
            headers["Link"] = '<%s>; rel="next"' % nxt
        return chunk, headers

    # ---------------------------------------------------------------- serializers

    def repo_json(self, repo):
        return {
            "id": repo.id,
            "node_id": self._node_id("Repository", repo.id),
            "name": repo.name,
            "full_name": "%s/%s" % (repo.owner, repo.name),
            "owner": {"login": repo.owner, "id": VIEWER["id"] if repo.owner == VIEWER["login"] else 0,
                      "type": "User"},
            "private": repo.private,
            "description": repo.description,
            "fork": False,
            "created_at": repo.created_at,
            "updated_at": repo.updated_at,
            "pushed_at": repo.updated_at,
            "homepage": repo.homepage,
            "size": 0,
            "stargazers_count": 0,
            "watchers_count": 0,
            "language": None,
            "has_issues": True,
            "has_projects": True,
            "has_wiki": True,
            "has_pages": False,
            "has_downloads": True,
            "archived": False,
            "disabled": False,
            "open_issues_count": sum(1 for i in repo.issues.values() if i["state"] == "open"),
            "license": None,
            "topics": [],
            "default_branch": repo.default_branch,
            "permissions": {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True},
            "url": "/api/v3/repos/%s/%s" % (repo.owner, repo.name),
            "html_url": "/%s/%s" % (repo.owner, repo.name),
            "clone_url": "/%s/%s.git" % (repo.owner, repo.name),
            "git_url": "/%s/%s.git" % (repo.owner, repo.name),
            "ssh_url": "git@forge:%s/%s.git" % (repo.owner, repo.name),
        }

    def issue_json(self, repo, issue):
        cached = issue.get("_json")
        if cached is not None:
            return cached
        obj = {
            "id": issue["id"],
            "node_id": self._node_id("Issue", "%d:%d" % (repo.id, issue["number"])),
            "number": issue["number"],
            "title": issue["title"],
            "body": issue["body"],
            "state": issue["state"],
            "labels": [{"name": name} for name in issue["labels"]],
            "user": {"login": VIEWER["login"], "id": VIEWER["id"], "type": "User"},
            "assignee": None,
            "assignees": [],
            "comments": len(issue["comments"]),
            "created_at": BASE_ISO,
            "updated_at": BASE_ISO,
            "closed_at": BASE_ISO if issue["state"] == "closed" else None,
            "url": "/api/v3/repos/%s/%s/issues/%d" % (repo.owner, repo.name, issue["number"]),
            "html_url": "/%s/%s/issues/%d" % (repo.owner, repo.name, issue["number"]),
            "repository_url": "/api/v3/repos/%s/%s" % (repo.owner, repo.name),
        }
        issue["_json"] = obj
        return obj

    def pr_json(self, repo, pr):
        head_tip = repo.refs.get("refs/heads/" + pr["head"])
        base_tip = repo.refs.get("refs/heads/" + pr["base"])
        state = pr["state"]
        gql_state = {"open": "OPEN", "closed": "MERGED" if pr["merged"] else "CLOSED"}[state]
        return {
            "id": pr["id"],
            "node_id": self._node_id("PullRequest", "%d:%d" % (repo.id, pr["number"])),
            "number": pr["number"],
            "title": pr["title"],
            "body": pr["body"],
            "state": state,
            "merged": pr["merged"],
            "merged_at": pr["merged_at"],
            "merge_commit_sha": pr["merge_commit_sha"],
            "head": {"ref": pr["head"], "sha": head_tip or ZERO,
                     "label": "%s:%s" % (repo.owner, pr["head"]),
                     "user": {"login": repo.owner, "id": VIEWER["id"]},
                     "repo": {"name": repo.name, "full_name": "%s/%s" % (repo.owner, repo.name)}},
            "base": {"ref": pr["base"], "sha": base_tip or ZERO,
                     "label": "%s:%s" % (repo.owner, pr["base"]),
                     "user": {"login": repo.owner, "id": VIEWER["id"]},
                     "repo": {"name": repo.name, "full_name": "%s/%s" % (repo.owner, repo.name)}},
            "user": {"login": VIEWER["login"], "id": VIEWER["id"], "type": "User"},
            "created_at": BASE_ISO,
            "updated_at": BASE_ISO,
            "closed_at": BASE_ISO if state == "closed" else None,
            "url": "/api/v3/repos/%s/%s/pulls/%d" % (repo.owner, repo.name, pr["number"]),
            "html_url": "/%s/%s/pull/%d" % (repo.owner, repo.name, pr["number"]),
            "_gql_state": gql_state,
        }

    @staticmethod
    def _node_id(kind, key):
        raw = ("%s:%s" % (kind, key)).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    # ---------------------------------------------------------------- routing

    def handle(self, method, parts, query, headers, body, base_url):
        """parts: path segments after /api/v3 (already URL-decoded).
        Returns (status, extra_headers, body_bytes). Raises ApiError."""
        self._check_auth(headers)
        if not parts or parts == [""]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            return 200, {"X-OAuth-Scopes": "repo, read:org"}, {
                "current_user_url": "/api/v3/user",
                "rate_limit_url": "/api/v3/rate_limit",
                "repository_url": "/api/v3/repos/{owner}/{repo}",
            }
        if parts == ["meta"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            return 200, {}, {"installed_version": "1.0.0-forge"}
        if parts == ["rate_limit"]:
            return 200, {}, {"rate": {"limit": 60, "remaining": 59, "reset": 0, "used": 1},
                            "resources": {"core": {"limit": 60, "remaining": 59, "reset": 0, "used": 1}}}
        if parts == ["user"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            return 200, {}, {"login": VIEWER["login"], "id": VIEWER["id"],
                            "name": VIEWER["name"], "email": VIEWER["email"], "type": "User"}
        if parts[:2] == ["user", "repos"]:
            return self._user_repos(method, query, body, base_url)
        if parts[0] == "repos" and len(parts) >= 3:
            return self._repo_routes(method, parts[1:], query, body)
        raise ApiError(404, "Not Found")

    def _user_repos(self, method, query, body, base_url):
        if method == "GET":
            repos = self.store.repos_for_owner(VIEWER["login"])
            chunk, extra = self._paginate([self.repo_json(r) for r in repos], query, base_url)
            return 200, extra, chunk
        if method == "POST":
            data = self._parse_body(body)
            name = data.get("name")
            if not name or not isinstance(name, str):
                raise ApiError(422, "name is required")
            if self.store.get_repo(VIEWER["login"], name) is not None:
                raise ApiError(422, "Repository already exists")
            repo = self.store.create_repo(
                VIEWER["login"], name,
                description=data.get("description") or "",
                private=bool(data.get("private", False)),
                default_branch=data.get("default_branch") or "main")
            return 201, {}, self.repo_json(repo)
        raise ApiError(404, "Not Found")

    def _repo_routes(self, method, parts, query, body):
        owner, name = parts[0], parts[1]
        rest = parts[2:]
        if not rest:
            if method == "GET":
                return 200, {}, self.repo_json(self._owner_repo(owner, name))
            if method == "PATCH":
                repo = self._owner_repo(owner, name)
                data = self._parse_body(body)
                if "description" in data:
                    repo.description = data["description"]
                if "default_branch" in data and data["default_branch"]:
                    repo.default_branch = data["default_branch"]
                repo.updated_at = BASE_ISO
                return 200, {}, self.repo_json(repo)
            if method == "DELETE":
                repo = self._owner_repo(owner, name)
                self.store.delete_repo(owner, name)
                return 204, {}, None
            raise ApiError(404, "Not Found")
        if rest == ["git", "refs"]:
            return self._git_refs(method, owner, name, body)
        if rest[:2] == ["git", "refs"]:
            return self._git_ref_one(method, owner, name, "/".join(rest[2:]))
        if rest == ["branches"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            repo = self._owner_repo(owner, name)
            out = []
            for refname in sorted(repo.refs):
                if refname.startswith("refs/heads/"):
                    out.append({"name": refname[len("refs/heads/"):],
                                "commit": {"sha": repo.refs[refname]},
                                "protected": False})
            return 200, {}, out
        if rest[:1] == ["branches"] and len(rest) == 2:
            if method != "GET":
                raise ApiError(404, "Not Found")
            repo = self._owner_repo(owner, name)
            tip = repo.refs.get("refs/heads/" + rest[1])
            if tip is None:
                raise ApiError(404, "Not Found")
            return 200, {}, {"name": rest[1], "commit": {"sha": tip}, "protected": False}
        if rest == ["commits"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            return self._commits(owner, name, query)
        if rest[0] == "contents":
            return self._contents(method, owner, name, "/".join(rest[1:]), query, body)
        if rest == ["labels"]:
            if method != "POST":
                raise ApiError(404, "Not Found")
            repo = self._owner_repo(owner, name)
            data = self._parse_body(body)
            label = data.get("name")
            if not label:
                raise ApiError(422, "name is required")
            if label in repo.labels:
                raise ApiError(422, "Label already exists")
            repo.labels[label] = {"name": label, "color": data.get("color") or "ededed"}
            return 201, {}, dict(repo.labels[label])
        if rest == ["issues"]:
            return self._issues(method, owner, name, query, body)
        if rest[:1] == ["issues"] and len(rest) == 2:
            return self._issue_one(method, owner, name, rest[1], body)
        if rest[:1] == ["issues"] and len(rest) == 3 and rest[2] == "comments":
            return self._issue_comments(method, owner, name, rest[1], body)
        if rest == ["pulls"]:
            return self._pulls(method, owner, name, query, body)
        if rest[:1] == ["pulls"] and len(rest) == 2:
            return self._pull_one(method, owner, name, rest[1])
        if rest[:1] == ["pulls"] and len(rest) == 3 and rest[2] == "files":
            return self._pull_files(owner, name, rest[1])
        if rest[:1] == ["pulls"] and len(rest) == 3 and rest[2] == "merge":
            return self._pull_merge(method, owner, name, rest[1], body)
        raise ApiError(404, "Not Found")

    # ---------------------------------------------------------------- git refs / branches / commits

    def _git_refs(self, method, owner, name, body):
        repo = self._owner_repo(owner, name)
        if method == "GET":
            out = []
            for refname in sorted(repo.refs):
                out.append({"ref": refname, "object": {"sha": repo.refs[refname], "type": "commit"}})
            return 200, {}, out
        if method == "POST":
            data = self._parse_body(body)
            ref = data.get("ref")
            sha = data.get("sha")
            if not ref or not isinstance(ref, str) or not ref.startswith("refs/"):
                raise ApiError(422, "ref must start with refs/")
            if not sha or sha not in repo.objects:
                raise ApiError(422, "sha does not exist")
            if ref in repo.refs:
                raise ApiError(422, "Reference already exists")
            repo.refs[ref] = sha
            return 201, {}, {"ref": ref, "object": {"sha": sha, "type": "commit"}}
        raise ApiError(404, "Not Found")

    def _git_ref_one(self, method, owner, name, ref):
        repo = self._owner_repo(owner, name)
        if method != "GET":
            raise ApiError(404, "Not Found")
        full = "refs/" + ref if not ref.startswith("refs/") else ref
        tip = repo.refs.get(full)
        if tip is None:
            raise ApiError(404, "Not Found")
        return 200, {}, {"ref": full, "object": {"sha": tip, "type": "commit"}}

    def _commits(self, owner, name, query):
        repo = self._owner_repo(owner, name)
        sha_param = query.get("sha", [None])[0]
        if sha_param:
            tip = repo.refs.get("refs/heads/" + sha_param) or repo.refs.get(sha_param) or sha_param
            if tip not in repo.objects and sha_param not in repo.objects:
                tip = None
        else:
            tip = repo.refs.get("refs/heads/" + repo.default_branch)
        out = []
        if tip:
            for csha in vcs.list_commits(repo.objects, tip):
                c = gitobj.parse_commit(repo.objects[csha][1])
                out.append({"sha": csha,
                            "commit": {"message": c["message"].decode("utf-8", "replace"),
                                       "author": {"name": VIEWER["name"], "email": VIEWER["email"]},
                                       "committer": {"name": VIEWER["name"], "email": VIEWER["email"]}},
                            "parents": [{"sha": p} for p in c["parents"]]})
        return 200, {}, out

    # ---------------------------------------------------------------- contents

    def _contents(self, method, owner, name, path, query, body):
        repo = self._owner_repo(owner, name)
        if method == "GET":
            ref = query.get("ref", [None])[0]
            tip = None
            if ref:
                tip = repo.refs.get("refs/heads/" + ref) or repo.refs.get(ref) or ref
                if tip not in repo.objects:
                    tip = None
            else:
                tip = repo.refs.get("refs/heads/" + repo.default_branch)
            if tip is None:
                raise ApiError(404, "Not Found")
            tree_sha = vcs.commit_tree_at(repo.objects, tip)
            mapping = vcs.flatten_tree(repo.objects, tree_sha)
            if path == "":
                return 200, {}, self._dir_listing(repo, mapping, "")
            if path in mapping:
                blob_sha = mapping[path]
                raw = repo.objects[blob_sha][1]
                return 200, {}, {"type": "file", "encoding": "base64",
                                "content": base64.b64encode(raw).decode(),
                                "sha": blob_sha, "size": len(raw),
                                "name": path.split("/")[-1], "path": path}
            prefix = path.rstrip("/") + "/"
            children = [p for p in mapping if p.startswith(prefix)]
            if not children:
                # maybe a directory with no files? treat as 404
                raise ApiError(404, "Not Found")
            return 200, {}, self._dir_listing(repo, mapping, path)
        if method in ("PUT", "DELETE"):
            data = self._parse_body(body)
            branch = data.get("branch") or repo.default_branch
            tip = repo.refs.get("refs/heads/" + branch)
            if tip is None and repo.refs:
                raise ApiError(422, "Branch does not exist")
            tree_sha = vcs.commit_tree_at(repo.objects, tip)
            mapping = vcs.flatten_tree(repo.objects, tree_sha)
            message = data.get("message") or ("Update %s" % path)
            if method == "PUT":
                content = _b64decode(data.get("content") or "")
                blob_sha = gitobj.obj_id(OBJ_BLOB, content)
                repo.objects[blob_sha] = (OBJ_BLOB, content)
                is_new = path not in mapping
                if not is_new:
                    given = data.get("sha")
                    if given and given != mapping[path]:
                        raise ApiError(409, "sha does not match")
                mapping[path] = blob_sha
                new_tree = vcs.build_tree(repo.objects, mapping)
                parents = [tip] if tip else []
                commit_sha = vcs.make_commit(repo.objects, new_tree, parents, message)
                repo.refs["refs/heads/" + branch] = commit_sha
                repo.updated_at = BASE_ISO
                return (201 if is_new else 200), {}, {
                    "content": {"name": path.split("/")[-1], "path": path, "sha": blob_sha,
                                "size": len(content), "encoding": "base64"},
                    "commit": {"sha": commit_sha}}
            # DELETE
            if path not in mapping:
                raise ApiError(404, "Not Found")
            given = data.get("sha")
            if given and given != mapping[path]:
                raise ApiError(409, "sha does not match")
            del mapping[path]
            new_tree = vcs.build_tree(repo.objects, mapping)
            parents = [tip] if tip else []
            commit_sha = vcs.make_commit(repo.objects, new_tree, parents, message)
            repo.refs["refs/heads/" + branch] = commit_sha
            repo.updated_at = BASE_ISO
            return 200, {}, {"commit": {"sha": commit_sha}}
        raise ApiError(404, "Not Found")

    def _dir_listing(self, repo, mapping, path):
        prefix = (path.rstrip("/") + "/") if path else ""
        seen = {}
        for p, sha in mapping.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            first = rest.split("/")[0]
            if "/" in rest:
                key = prefix + first
                seen[key] = {"type": "dir", "name": first, "path": key,
                             "sha": self._subdir_tree_sha(repo, prefix + first) or ""}
            else:
                seen[p] = {"type": "file", "name": first, "path": p, "sha": sha,
                           "size": len(repo.objects[sha][1])}
        return [seen[k] for k in sorted(seen)]

    def _subdir_tree_sha(self, repo, dirpath):
        tip = repo.refs.get("refs/heads/" + repo.default_branch)
        tree_sha = vcs.commit_tree_at(repo.objects, tip)
        parts = dirpath.split("/")
        cur = tree_sha
        for part in parts:
            if cur is None:
                return None
            entry = repo.objects.get(cur)
            if entry is None:
                return None
            found = None
            for mode, nm, sha in gitobj.parse_tree(entry[1]):
                if nm.decode("utf-8", "surrogateescape") == part and mode == b"40000":
                    found = sha
                    break
            cur = found
        return cur

    # ---------------------------------------------------------------- issues

    def _issues(self, method, owner, name, query, body):
        repo = self._owner_repo(owner, name)
        if method == "GET":
            state = query.get("state", ["open"])[0]
            sort = query.get("sort", ["created"])[0]
            direction = query.get("direction", ["desc"])[0]
            items = list(repo.issues.values())
            if state == "open":
                items = [i for i in items if i["state"] == "open"]
            elif state == "closed":
                items = [i for i in items if i["state"] == "closed"]
            if sort == "created":
                items.sort(key=lambda i: i["created_seq"], reverse=(direction == "desc"))
            chunk, _extra = self._paginate([self.issue_json(repo, i) for i in items], query, "")
            return 200, {}, chunk
        if method == "POST":
            data = self._parse_body(body)
            title = data.get("title")
            if not title:
                raise ApiError(422, "title is required")
            labels = data.get("labels") or []
            for lab in labels:
                lab_name = lab if isinstance(lab, str) else lab.get("name")
                if lab_name not in repo.labels:
                    raise ApiError(422, "Label %s does not exist" % lab_name)
            repo.issue_seq += 1
            number = repo.issue_seq
            issue = {"id": self.store.next_id(), "number": number,
                     "title": title, "body": data.get("body") or "",
                     "state": "open",
                     "labels": [l if isinstance(l, str) else l.get("name") for l in labels],
                     "comments": [], "created_seq": self.store.next_seq()}
            repo.issues[number] = issue
            return 201, {}, self.issue_json(repo, issue)
        raise ApiError(404, "Not Found")

    def _issue_one(self, method, owner, name, number_s, body):
        repo = self._owner_repo(owner, name)
        try:
            number = int(number_s)
        except ValueError:
            raise ApiError(404, "Not Found")
        issue = repo.issues.get(number)
        if issue is None:
            raise ApiError(404, "Not Found")
        if method == "GET":
            return 200, {}, self.issue_json(repo, issue)
        if method == "PATCH":
            data = self._parse_body(body)
            if "state" in data and data["state"] in ("open", "closed"):
                issue["state"] = data["state"]
            if "title" in data:
                issue["title"] = data["title"]
            if "body" in data:
                issue["body"] = data["body"]
            issue.pop("_json", None)
            return 200, {}, self.issue_json(repo, issue)
        raise ApiError(404, "Not Found")

    def _issue_comments(self, method, owner, name, number_s, body):
        repo = self._owner_repo(owner, name)
        try:
            number = int(number_s)
        except ValueError:
            raise ApiError(404, "Not Found")
        issue = repo.issues.get(number)
        if issue is None:
            raise ApiError(404, "Not Found")
        if method == "GET":
            return 200, {}, [{"id": c["id"], "body": c["body"]} for c in issue["comments"]]
        if method == "POST":
            data = self._parse_body(body)
            comment_body = data.get("body")
            if comment_body is None:
                raise ApiError(422, "body is required")
            repo.comment_seq = getattr(repo, "comment_seq", 0) + 1
            comment = {"id": repo.comment_seq, "body": comment_body}
            issue["comments"].append(comment)
            return 201, {}, {"id": comment["id"], "body": comment["body"]}
        raise ApiError(404, "Not Found")

    # ---------------------------------------------------------------- pulls

    def _pulls(self, method, owner, name, query, body):
        repo = self._owner_repo(owner, name)
        if method == "GET":
            state = query.get("state", ["open"])[0]
            items = list(repo.pulls.values())
            if state == "open":
                items = [p for p in items if p["state"] == "open"]
            elif state == "closed":
                items = [p for p in items if p["state"] == "closed"]
            items.sort(key=lambda p: p["created_seq"])
            chunk, _extra = self._paginate(
                [self._pr_public(repo, p) for p in items], query, "")
            return 200, {}, chunk
        if method == "POST":
            data = self._parse_body(body)
            title = data.get("title")
            head = data.get("head")
            base = data.get("base") or repo.default_branch
            if not title or not head:
                raise ApiError(422, "title and head are required")
            if ("refs/heads/" + head) not in repo.refs or ("refs/heads/" + base) not in repo.refs:
                raise ApiError(422, "head or base branch does not exist")
            for p in repo.pulls.values():
                if p["state"] == "open" and p["head"] == head and p["base"] == base:
                    raise ApiError(422, "A pull request already exists for this head and base")
            repo.pull_seq += 1
            number = repo.pull_seq
            pr = {"id": self.store.next_id(), "number": number,
                  "title": title, "body": data.get("body") or "",
                  "state": "open", "head": head, "base": base,
                  "base_sha": repo.refs["refs/heads/" + base],
                  "merged": False, "merged_at": None, "merge_commit_sha": None,
                  "created_seq": self.store.next_seq()}
            repo.pulls[number] = pr
            return 201, {}, self._pr_public(repo, pr)
        raise ApiError(404, "Not Found")

    def _pr_public(self, repo, pr):
        obj = self.pr_json(repo, pr)
        obj.pop("_gql_state", None)
        return obj

    def _pull_one(self, method, owner, name, number_s):
        repo = self._owner_repo(owner, name)
        if method != "GET":
            raise ApiError(404, "Not Found")
        try:
            number = int(number_s)
        except ValueError:
            raise ApiError(404, "Not Found")
        pr = repo.pulls.get(number)
        if pr is None:
            raise ApiError(404, "Not Found")
        return 200, {}, self._pr_public(repo, pr)

    def _pull_files(self, owner, name, number_s):
        repo = self._owner_repo(owner, name)
        try:
            number = int(number_s)
        except ValueError:
            raise ApiError(404, "Not Found")
        pr = repo.pulls.get(number)
        if pr is None:
            raise ApiError(404, "Not Found")
        base_tip = repo.refs.get("refs/heads/" + pr["base"])
        head_tip = repo.refs.get("refs/heads/" + pr["head"])
        base_tree = vcs.commit_tree_at(repo.objects, base_tip)
        head_tree = vcs.commit_tree_at(repo.objects, head_tip)
        files = vcs.diff_files(repo.objects, base_tree, head_tree)
        return 200, {}, [{"filename": f["path"], "status": f["status"],
                          "additions": f["additions"], "deletions": f["deletions"],
                          "changes": f["changes"]} for f in files]

    def _pull_merge(self, method, owner, name, number_s, body):
        repo = self._owner_repo(owner, name)
        if method != "PUT":
            raise ApiError(404, "Not Found")
        try:
            number = int(number_s)
        except ValueError:
            raise ApiError(404, "Not Found")
        pr = repo.pulls.get(number)
        if pr is None:
            raise ApiError(404, "Not Found")
        if pr["state"] != "open":
            raise ApiError(405, "Pull request is already closed")
        base_tip = repo.refs.get("refs/heads/" + pr["base"])
        head_tip = repo.refs.get("refs/heads/" + pr["head"])
        if base_tip is None or head_tip is None:
            raise ApiError(405, "Cannot merge: missing branch")
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
        return 200, {}, {"merged": True, "sha": merge_sha, "message": message}
