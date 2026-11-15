"""GitHub GraphQL v4 API at /api/graphql: a small parser + executor for the
documents gh and github-mcp-server send. Pure stdlib.
"""
import base64
import json

from . import vcs
from .rest import ApiError
from .store import VIEWER

# ---------------------------------------------------------------- global ids

def gid(kind, key):
    raw = ("%s:%s" % (kind, key)).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def ungid(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        kind, _, key = raw.partition(":")
        return kind, key
    except Exception:
        return None, None


def repo_gid(repo):
    return gid("Repository", str(repo.id))


def issue_gid(repo, issue):
    return gid("Issue", "%d:%d" % (repo.id, issue["number"]))


def pr_gid(repo, pr):
    return gid("PullRequest", "%d:%d" % (repo.id, pr["number"]))


def comment_gid(repo, issue, comment):
    return gid("Comment", "%d:%d:%d" % (repo.id, issue["number"], comment["id"]))


# ---------------------------------------------------------------- schema tables

TYPE_FIELDS = {
    "Query": {"viewer", "repository", "node", "__type", "__schema"},
    "Repository": {"id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                   "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests",
                   "pullRequest", "description", "createdAt", "updatedAt", "url"},
    "Issue": {"id", "number", "title", "state", "body", "createdAt", "updatedAt", "closedAt",
              "url", "author", "labels", "comments"},
    "PullRequest": {"id", "number", "title", "body", "state", "merged", "headRefName",
                    "baseRefName", "mergeable", "files", "createdAt", "updatedAt", "url"},
    "User": {"login", "id", "name", "email"},
    "BranchRef": {"name", "id"},
    "IssueConnection": {"nodes", "edges", "pageInfo", "totalCount"},
    "PullRequestConnection": {"nodes", "edges", "pageInfo", "totalCount"},
    "PullRequestChangedFileConnection": {"nodes", "edges", "pageInfo", "totalCount"},
    "IssueEdge": {"node", "cursor"},
    "PullRequestEdge": {"node", "cursor"},
    "PullRequestChangedFile": {"path", "filename"},
    "PageInfo": {"hasNextPage", "hasPreviousPage", "endCursor", "startCursor"},
    "__Type": {"name", "kind", "fields", "enumValues", "description"},
    "__Field": {"name", "description"},
    "__EnumValue": {"name", "description"},
    "CreateIssuePayload": {"issue", "clientMutationId"},
    "CloseIssuePayload": {"issue", "clientMutationId"},
    "CreatePullRequestPayload": {"pullRequest", "clientMutationId"},
    "MergePullRequestPayload": {"pullRequest", "clientMutationId"},
    "AddCommentPayload": {"commentEdge", "clientMutationId"},
    "IssueCommentEdge": {"node", "cursor"},
    "IssueComment": {"id", "body"},
    "Mutation": {"createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"},
}

INTROSPECTION = {
    "Issue": {"fields": ["id", "number", "title", "state", "body", "createdAt", "updatedAt",
                         "closedAt", "url", "author", "labels", "comments"],
              "enumValues": []},
    "PullRequest": {"fields": ["id", "number", "headRefName", "baseRefName", "mergeable",
                               "state", "merged", "files", "title", "body", "createdAt",
                               "updatedAt", "url"],
                    "enumValues": []},
    "Repository": {"fields": ["id", "databaseId", "name", "nameWithOwner", "owner",
                              "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                              "issue", "issues", "pullRequests", "pullRequest", "description",
                              "createdAt", "updatedAt", "url"],
                   "enumValues": []},
    "SearchType": {"fields": [], "enumValues": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION",
                                                "COMMIT", "CODE", "PULL_REQUEST"]},
    "IssueState": {"fields": [], "enumValues": ["OPEN", "CLOSED"]},
    "MergeableState": {"fields": [], "enumValues": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "PullRequestState": {"fields": [], "enumValues": ["OPEN", "CLOSED", "MERGED"]},
    "PullRequestMergeMethod": {"fields": [], "enumValues": ["MERGE", "SQUASH", "REBASE"]},
    "RepositoryPermission": {"fields": [], "enumValues": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"]},
    "IssueOrderField": {"fields": [], "enumValues": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "OrderDirection": {"fields": [], "enumValues": ["ASC", "DESC"]},
}

# ---------------------------------------------------------------- parser

class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value


def _tokenize(text):
    toks = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in "{}()[]:!=$@&|":
            toks.append(_Tok(c, c))
            i += 1
            continue
        if c == ".":
            if text[i:i + 3] == "...":
                toks.append(_Tok("...", "..."))
                i += 3
                continue
            raise ValueError("bad dot")
        if c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(_Tok("string", "".join(out)))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            toks.append(_Tok("number", float(raw) if any(ch in raw for ch in ".eE") else int(raw)))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(_Tok("name", text[i:j]))
            i = j
            continue
        raise ValueError("unexpected char %r" % c)
    toks.append(_Tok("eof", None))
    return toks


class _Parser:
    def __init__(self, text):
        self.toks = _tokenize(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind):
        t = self.next()
        if t.kind != kind:
            raise ValueError("expected %s, got %s" % (kind, t.kind))
        return t

    def skip(self, kind):
        if self.peek().kind == kind:
            self.next()
            return True
        return False

    def parse_document(self):
        ops = []
        frags = {}
        while self.peek().kind != "eof":
            if self.peek().kind == "{":
                ops.append({"kind": "query", "name": None, "vars": {}, "selection": self.parse_selection()})
            elif self.peek().kind == "name" and self.peek().value in ("query", "mutation", "subscription"):
                kind = self.next().value
                name = None
                if self.peek().kind == "name":
                    name = self.next().value
                vars_ = {}
                if self.skip("("):
                    vars_ = self.parse_var_defs()
                self.skip_directives()
                ops.append({"kind": kind, "name": name, "vars": vars_,
                            "selection": self.parse_selection()})
            elif self.peek().kind == "name" and self.peek().value == "fragment":
                self.next()
                fname = self.expect("name").value
                self.expect("name")  # "on"
                ftype = self.expect("name").value
                self.skip_directives()
                frags[fname] = {"type": ftype, "selection": self.parse_selection()}
            else:
                raise ValueError("unexpected token %s" % self.peek().kind)
        return ops, frags

    def skip_directives(self):
        while self.peek().kind == "@":
            self.next()
            self.expect("name")
            if self.skip("("):
                self.parse_args()

    def parse_var_defs(self):
        vars_ = {}
        while not self.skip(")"):
            self.expect("$")
            name = self.expect("name").value
            self.expect(":")
            vtype = self.parse_type()
            default = None
            if self.skip("="):
                default = self.parse_value({})
            vars_[name] = {"type": vtype, "default": default}
        return vars_

    def parse_type(self):
        if self.skip("["):
            inner = self.parse_type()
            self.expect("]")
            t = "[" + inner + "]"
        else:
            t = self.expect("name").value
        if self.skip("!"):
            t += "!"
        return t

    def parse_selection(self):
        self.expect("{")
        fields = []
        while not self.skip("}"):
            fields.append(self.parse_field())
        return fields

    def parse_field(self):
        if self.peek().kind == "...":
            self.next()
            if self.peek().kind == "name" and self.peek().value == "on":
                self.next()
                ftype = self.expect("name").value
                self.skip_directives()
                return {"kind": "inline", "type": ftype, "selection": self.parse_selection()}
            fname = self.expect("name").value
            self.skip_directives()
            return {"kind": "spread", "name": fname}
        name = self.expect("name").value
        alias = None
        if self.peek().kind == ":":
            self.next()
            alias = name
            name = self.expect("name").value
        args = {}
        if self.skip("("):
            args = self.parse_args()
        self.skip_directives()
        selection = []
        if self.peek().kind == "{":
            selection = self.parse_selection()
        return {"kind": "field", "name": name, "alias": alias or name,
                "args": args, "selection": selection}

    def parse_args(self):
        args = {}
        while not self.skip(")"):
            name = self.expect("name").value
            self.expect(":")
            args[name] = self.parse_value({})
        return args

    def parse_value(self, variables):
        t = self.peek()
        if t.kind == "$":
            self.next()
            return ("$var", self.expect("name").value)
        if t.kind == "string":
            self.next()
            return t.value
        if t.kind == "number":
            self.next()
            return t.value
        if t.kind == "[":
            self.next()
            out = []
            while not self.skip("]"):
                out.append(self.parse_value(variables))
            return out
        if t.kind == "{":
            self.next()
            out = {}
            while not self.skip("}"):
                name = self.expect("name").value
                self.expect(":")
                out[name] = self.parse_value(variables)
            return out
        if t.kind == "name":
            self.next()
            if t.value == "true":
                return True
            if t.value == "false":
                return False
            if t.value == "null":
                return None
            return ("$enum", t.value)
        raise ValueError("bad value %s" % t.kind)


def _resolve_value(value, variables):
    if isinstance(value, tuple) and value[0] == "$var":
        return variables.get(value[1])
    if isinstance(value, tuple) and value[0] == "$enum":
        return value[1]
    if isinstance(value, list):
        return [_resolve_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, variables) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- executor

class GraphQL:
    def __init__(self, store):
        self.store = store

    def execute(self, body, headers):
        """body: parsed JSON (dict or list). Returns (status, result_obj)."""
        if isinstance(body, list):
            return 200, [self._execute_one(item) for item in body]
        return 200, self._execute_one(body)

    def _execute_one(self, op):
        if not isinstance(op, dict) or "query" not in op:
            return {"data": None, "errors": [{"message": "Request must include a query"}]}
        query = op.get("query") or ""
        variables = op.get("variables") or {}
        op_name = op.get("operationName")
        try:
            ops, frags = _Parser(query).parse_document()
        except ValueError as e:
            return {"data": None, "errors": [{"message": "Syntax Error: %s" % e}]}
        if not ops:
            return {"data": None, "errors": [{"message": "No operation in document"}]}
        chosen = None
        if op_name:
            for o in ops:
                if o["name"] == op_name:
                    chosen = o
                    break
            if chosen is None:
                return {"data": None, "errors": [{"message": "Unknown operation %s" % op_name}]}
        else:
            chosen = ops[0]
        variables = dict(variables)
        for vname, vdef in chosen["vars"].items():
            if vname not in variables and vdef.get("default") is not None:
                variables[vname] = _resolve_value(vdef["default"], variables)
        ctx = {"errors": [], "frags": frags, "vars": variables}
        if chosen["kind"] == "mutation":
            data = self._resolve_selection("Mutation", None, chosen["selection"], ctx)
        else:
            data = self._resolve_selection("Query", None, chosen["selection"], ctx)
        out = {"data": data}
        if ctx["errors"]:
            out["errors"] = ctx["errors"]
        return out

    # -- selection resolution -------------------------------------------------

    def _resolve_selection(self, typename, obj, selection, ctx):
        data = {}
        for field in selection:
            if field["kind"] == "spread":
                frag = ctx["frags"].get(field["name"])
                if frag is None:
                    continue
                if frag["type"] != typename:
                    continue
                data.update(self._resolve_selection(typename, obj, frag["selection"], ctx))
                continue
            if field["kind"] == "inline":
                if field.get("type") and field["type"] != typename:
                    continue
                data.update(self._resolve_selection(typename, obj, field["selection"], ctx))
                continue
            name = field["name"]
            allowed = TYPE_FIELDS.get(typename, set())
            if name not in allowed:
                ctx["errors"].append(
                    {"message": 'Cannot query field "%s" on type "%s".' % (name, typename)})
                continue
            args = {k: _resolve_value(v, ctx["vars"]) for k, v in field["args"].items()}
            value = self._resolve_field(typename, obj, name, args, ctx)
            if field["selection"]:
                value = self._resolve_child(value, field["selection"], ctx)
            data[field["alias"]] = value
        return data

    def _resolve_child(self, value, selection, ctx):
        if value is None:
            return None
        if isinstance(value, list):
            return [self._resolve_child(v, selection, ctx) for v in value]
        if not (isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str)):
            return value
        typename, obj = value
        return self._resolve_selection(typename, obj, selection, ctx)

    # -- field resolution -----------------------------------------------------

    def _resolve_field(self, typename, obj, name, args, ctx):
        if typename == "Query":
            return self._query_field(name, args, ctx)
        if typename == "Mutation":
            return self._mutation_field(name, args, ctx)
        if typename == "Repository":
            return self._repo_field(obj, name, args, ctx)
        if typename == "Issue":
            return self._issue_field(obj, name, args, ctx)
        if typename == "PullRequest":
            return self._pr_field(obj, name, args, ctx)
        if typename == "User":
            return obj.get(name)
        if typename == "BranchRef":
            return obj["name"] if name == "name" else gid("BranchRef", obj["name"])
        if typename in ("IssueConnection", "PullRequestConnection",
                        "PullRequestChangedFileConnection"):
            return self._connection_field(obj, name, args, ctx)
        if typename in ("IssueEdge", "PullRequestEdge", "IssueCommentEdge"):
            if name == "node":
                return obj[0]
            return obj[1]
        if typename == "PageInfo":
            return obj.get(name)
        if typename == "__Type":
            return self._type_field(obj, name, args, ctx)
        if typename in ("__Field", "__EnumValue"):
            return obj.get(name)
        if typename in ("CreateIssuePayload", "CloseIssuePayload"):
            return obj.get(name)
        if typename == "CreatePullRequestPayload":
            return obj.get(name)
        if typename == "MergePullRequestPayload":
            return obj.get(name)
        if typename == "AddCommentPayload":
            return obj.get(name)
        if typename == "IssueComment":
            return obj.get(name)
        if typename == "PullRequestChangedFile":
            return obj.get(name)
        return None

    def _query_field(self, name, args, ctx):
        if name == "viewer":
            return ("User", VIEWER)
        if name == "repository":
            owner = args.get("owner")
            rname = args.get("name")
            if owner != VIEWER["login"]:
                return None
            repo = self.store.get_repo(owner, rname)
            return ("Repository", repo) if repo else None
        if name == "node":
            kind, key = ungid(args.get("id") or "")
            if kind == "Repository":
                try:
                    rid = int(key)
                except ValueError:
                    return None
                for repo in self.store.all_repos():
                    if repo.id == rid:
                        return ("Repository", repo)
                return None
            if kind == "Issue":
                parts = key.split(":")
                if len(parts) != 2:
                    return None
                repo = next((r for r in self.store.all_repos() if r.id == int(parts[0])), None)
                if repo is None:
                    return None
                issue = repo.issues.get(int(parts[1]))
                return ("Issue", (repo, issue)) if issue else None
            if kind == "PullRequest":
                parts = key.split(":")
                if len(parts) != 2:
                    return None
                repo = next((r for r in self.store.all_repos() if r.id == int(parts[0])), None)
                if repo is None:
                    return None
                pr = repo.pulls.get(int(parts[1]))
                return ("PullRequest", (repo, pr)) if pr else None
            return None
        if name == "__type":
            tname = args.get("name")
            if tname in INTROSPECTION:
                return ("__Type", {"name": tname})
            return None
        return None

    def _repo_field(self, repo, name, args, ctx):
        if name == "id":
            return repo_gid(repo)
        if name == "databaseId":
            return repo.id
        if name == "name":
            return repo.name
        if name == "nameWithOwner":
            return "%s/%s" % (repo.owner, repo.name)
        if name == "owner":
            return ("User", {"login": repo.owner, "id": VIEWER["id"]})
        if name == "defaultBranchRef":
            return ("BranchRef", {"name": repo.default_branch})
        if name == "viewerPermission":
            return "ADMIN"
        if name == "hasIssuesEnabled":
            return True
        if name == "description":
            return repo.description
        if name == "createdAt" or name == "updatedAt":
            from .store import BASE_ISO
            return BASE_ISO
        if name == "url":
            return "/%s/%s" % (repo.owner, repo.name)
        if name == "issue":
            issue = repo.issues.get(args.get("number"))
            return ("Issue", (repo, issue)) if issue else None
        if name == "issues":
            return ("IssueConnection", self._issues_connection(repo, args))
        if name == "pullRequests":
            return ("PullRequestConnection", self._prs_connection(repo, args))
        if name == "pullRequest":
            pr = repo.pulls.get(args.get("number"))
            return ("PullRequest", (repo, pr)) if pr else None
        return None

    def _issues_connection(self, repo, args):
        items = list(repo.issues.values())
        states = args.get("states")
        if states:
            want = {"OPEN": "open", "CLOSED": "closed"}
            items = [i for i in items if i["state"] in
                     {want.get(s, s.lower()) for s in states}]
        order_by = args.get("orderBy") or {}
        field = (order_by.get("field") or "CREATED_AT").upper()
        direction = (order_by.get("direction") or "ASC").upper()
        if field == "CREATED_AT":
            items.sort(key=lambda i: (i["created_seq"], i["number"]),
                       reverse=(direction == "DESC"))
        elif field == "UPDATED_AT":
            items.sort(key=lambda i: (i["created_seq"], i["number"]),
                       reverse=(direction == "DESC"))
        elif field == "COMMENTS":
            items.sort(key=lambda i: (len(i["comments"]), i["number"]),
                       reverse=(direction == "DESC"))
        else:
            items.sort(key=lambda i: i["number"], reverse=(direction == "DESC"))
        first = args.get("first")
        after = args.get("after")
        start = 0
        if after:
            try:
                padded = after + "=" * (-len(after) % 4)
                raw = base64.urlsafe_b64decode(padded).decode()
                start = int(raw.split(":")[1]) + 1
            except Exception:
                start = 0
        total = len(items)
        if first is None:
            page = items[start:]
        else:
            page = items[start:start + first]
        end = start + len(page)
        nodes = [("Issue", (repo, i)) for i in page]
        end_cursor = None
        if page:
            raw = ("cursor:%d" % (end - 1)).encode()
            end_cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return {"nodes": nodes, "total": total, "edge": "IssueEdge",
                "pageInfo": {"hasNextPage": end < total,
                             "hasPreviousPage": start > 0,
                             "endCursor": end_cursor,
                             "startCursor": end_cursor}}

    def _prs_connection(self, repo, args):
        items = sorted(repo.pulls.values(), key=lambda p: p["number"])
        first = args.get("first")
        if first is not None:
            items = items[:first]
        nodes = [("PullRequest", (repo, p)) for p in items]
        return {"nodes": nodes, "total": len(repo.pulls), "edge": "PullRequestEdge",
                "pageInfo": {"hasNextPage": False, "hasPreviousPage": False,
                             "endCursor": None, "startCursor": None}}

    def _issue_field(self, pair, name, args, ctx):
        repo, issue = pair
        if issue is None:
            return None
        if name == "id":
            return issue_gid(repo, issue)
        if name == "number":
            return issue["number"]
        if name == "title":
            return issue["title"]
        if name == "body":
            return issue["body"]
        if name == "state":
            return issue["state"].upper()
        if name in ("createdAt", "updatedAt"):
            from .store import BASE_ISO
            return BASE_ISO
        if name == "closedAt":
            from .store import BASE_ISO
            return BASE_ISO if issue["state"] == "closed" else None
        if name == "url":
            return "/%s/%s/issues/%d" % (repo.owner, repo.name, issue["number"])
        if name == "author":
            return ("User", VIEWER)
        if name == "labels":
            return [{"name": n} for n in issue["labels"]]
        if name == "comments":
            nodes = [(("IssueComment", c)) for c in issue["comments"]]
            return ("IssueConnection", {"nodes": nodes, "total": len(nodes),
                                        "edge": "IssueCommentEdge",
                                        "pageInfo": {"hasNextPage": False, "hasPreviousPage": False,
                                                     "endCursor": None, "startCursor": None}})
        return None

    def _pr_field(self, pair, name, args, ctx):
        repo, pr = pair
        if pr is None:
            return None
        if name == "id":
            return pr_gid(repo, pr)
        if name == "number":
            return pr["number"]
        if name == "title":
            return pr["title"]
        if name == "body":
            return pr["body"]
        if name == "state":
            return "MERGED" if pr["merged"] else pr["state"].upper()
        if name == "merged":
            return pr["merged"]
        if name == "headRefName":
            return pr["head"]
        if name == "baseRefName":
            return pr["base"]
        if name == "mergeable":
            base_tip = repo.refs.get("refs/heads/" + pr["base"])
            head_tip = repo.refs.get("refs/heads/" + pr["head"])
            if base_tip is None or head_tip is None:
                return "UNKNOWN"
            if pr["state"] != "open":
                return "UNKNOWN"
            return "CONFLICTING" if vcs.pr_conflicts(repo.objects, pr, base_tip, head_tip) else "MERGEABLE"
        if name == "files":
            base_tip = repo.refs.get("refs/heads/" + pr["base"])
            head_tip = repo.refs.get("refs/heads/" + pr["head"])
            base_tree = vcs.commit_tree_at(repo.objects, base_tip)
            head_tree = vcs.commit_tree_at(repo.objects, head_tip)
            files = vcs.diff_files(repo.objects, base_tree, head_tree)
            first = args.get("first")
            if first is not None:
                files = files[:first]
            nodes = [("PullRequestChangedFile", {"path": f["path"]}) for f in files]
            return ("PullRequestChangedFileConnection",
                    {"nodes": nodes, "total": len(nodes),
                     "pageInfo": {"hasNextPage": False, "hasPreviousPage": False,
                                  "endCursor": None, "startCursor": None}})
        if name in ("createdAt", "updatedAt"):
            from .store import BASE_ISO
            return BASE_ISO
        if name == "url":
            return "/%s/%s/pull/%d" % (repo.owner, repo.name, pr["number"])
        return None

    def _connection_field(self, conn, name, args, ctx):
        if name == "nodes":
            return conn["nodes"]
        if name == "edges":
            edge_type = conn.get("edge", "IssueEdge")
            return [(edge_type, (node, None)) for node in conn["nodes"]]
        if name == "pageInfo":
            return ("PageInfo", conn["pageInfo"])
        if name == "totalCount":
            return conn["total"]
        return None

    def _type_field(self, type_obj, name, args, ctx):
        tname = type_obj["name"]
        if name == "name":
            return tname
        if name == "kind":
            return "OBJECT" if INTROSPECTION[tname]["fields"] else "ENUM"
        if name == "fields":
            return [("__Field", {"name": f}) for f in INTROSPECTION[tname]["fields"]]
        if name == "enumValues":
            return [("__EnumValue", {"name": v}) for v in INTROSPECTION[tname]["enumValues"]]
        if name == "description":
            return None
        return None

    # -- mutations ------------------------------------------------------------

    def _mutation_field(self, name, args, ctx):
        inp = args.get("input") or {}
        if name == "createIssue":
            return ("CreateIssuePayload", self._m_create_issue(inp, ctx))
        if name == "closeIssue":
            return ("CloseIssuePayload", self._m_close_issue(inp, ctx))
        if name == "createPullRequest":
            return ("CreatePullRequestPayload", self._m_create_pr(inp, ctx))
        if name == "mergePullRequest":
            return ("MergePullRequestPayload", self._m_merge_pr(inp, ctx))
        if name == "addComment":
            return ("AddCommentPayload", self._m_add_comment(inp, ctx))
        return None

    def _find_repo_by_gid(self, gid_value):
        kind, key = ungid(gid_value or "")
        if kind != "Repository":
            return None
        try:
            rid = int(key)
        except ValueError:
            return None
        for repo in self.store.all_repos():
            if repo.id == rid:
                return repo
        return None

    def _m_create_issue(self, inp, ctx):
        repo = self._find_repo_by_gid(inp.get("repositoryId"))
        if repo is None:
            ctx["errors"].append({"message": "Repository not found"})
            return {"issue": None}
        title = inp.get("title") or ""
        repo.issue_seq += 1
        number = repo.issue_seq
        issue = {"id": self.store.next_id(), "number": number, "title": title,
                 "body": inp.get("body") or "", "state": "open", "labels": [],
                 "comments": [], "created_seq": self.store.next_seq()}
        repo.issues[number] = issue
        return {"issue": ("Issue", (repo, issue))}

    def _m_close_issue(self, inp, ctx):
        kind, key = ungid(inp.get("issueId") or "")
        if kind != "Issue":
            ctx["errors"].append({"message": "Issue not found"})
            return {"issue": None}
        parts = key.split(":")
        repo = next((r for r in self.store.all_repos() if r.id == int(parts[0])), None)
        issue = repo.issues.get(int(parts[1])) if repo else None
        if issue is None:
            ctx["errors"].append({"message": "Issue not found"})
            return {"issue": None}
        issue["state"] = "closed"
        issue.pop("_json", None)
        return {"issue": ("Issue", (repo, issue))}

    def _m_create_pr(self, inp, ctx):
        repo = self._find_repo_by_gid(inp.get("repositoryId"))
        if repo is None:
            ctx["errors"].append({"message": "Repository not found"})
            return {"pullRequest": None}
        head = inp.get("headRefName")
        base = inp.get("baseRefName") or repo.default_branch
        if ("refs/heads/" + head) not in repo.refs or ("refs/heads/" + base) not in repo.refs:
            ctx["errors"].append({"message": "head or base branch does not exist"})
            return {"pullRequest": None}
        for p in repo.pulls.values():
            if p["state"] == "open" and p["head"] == head and p["base"] == base:
                ctx["errors"].append({"message": "A pull request already exists for this head and base"})
                return {"pullRequest": None}
        repo.pull_seq += 1
        number = repo.pull_seq
        pr = {"id": self.store.next_id(), "number": number,
              "title": inp.get("title") or "", "body": inp.get("body") or "",
              "state": "open", "head": head, "base": base,
              "base_sha": repo.refs["refs/heads/" + base],
              "merged": False, "merged_at": None, "merge_commit_sha": None,
              "created_seq": self.store.next_seq()}
        repo.pulls[number] = pr
        return {"pullRequest": ("PullRequest", (repo, pr))}

    def _m_merge_pr(self, inp, ctx):
        kind, key = ungid(inp.get("pullRequestId") or "")
        if kind != "PullRequest":
            ctx["errors"].append({"message": "Pull request not found"})
            return {"pullRequest": None}
        parts = key.split(":")
        repo = next((r for r in self.store.all_repos() if r.id == int(parts[0])), None)
        pr = repo.pulls.get(int(parts[1])) if repo else None
        if pr is None:
            ctx["errors"].append({"message": "Pull request not found"})
            return {"pullRequest": None}
        if pr["state"] != "open":
            ctx["errors"].append({"message": "Pull request is already closed"})
            return {"pullRequest": ("PullRequest", (repo, pr))}
        base_tip = repo.refs.get("refs/heads/" + pr["base"])
        head_tip = repo.refs.get("refs/heads/" + pr["head"])
        if base_tip is None or head_tip is None or vcs.pr_conflicts(repo.objects, pr, base_tip, head_tip):
            ctx["errors"].append({"message": "Merge conflict"})
            return {"pullRequest": ("PullRequest", (repo, pr))}
        head_tree = vcs.commit_tree_at(repo.objects, head_tip)
        message = "Merge pull request #%d from %s/%s" % (pr["number"], repo.owner, pr["head"])
        merge_sha = vcs.make_commit(repo.objects, head_tree, [base_tip], message)
        repo.refs["refs/heads/" + pr["base"]] = merge_sha
        pr["state"] = "closed"
        pr["merged"] = True
        from .store import BASE_ISO
        pr["merged_at"] = BASE_ISO
        pr["merge_commit_sha"] = merge_sha
        return {"pullRequest": ("PullRequest", (repo, pr))}

    def _m_add_comment(self, inp, ctx):
        kind, key = ungid(inp.get("subjectId") or "")
        if kind != "Issue":
            ctx["errors"].append({"message": "Subject not found"})
            return {"commentEdge": None}
        parts = key.split(":")
        repo = next((r for r in self.store.all_repos() if r.id == int(parts[0])), None)
        issue = repo.issues.get(int(parts[1])) if repo else None
        if issue is None:
            ctx["errors"].append({"message": "Subject not found"})
            return {"commentEdge": None}
        repo.comment_seq += 1
        comment = {"id": repo.comment_seq, "body": inp.get("body") or ""}
        issue["comments"].append(comment)
        node = ("IssueComment", {"id": comment_gid(repo, issue, comment),
                                 "body": comment["body"]})
        return {"commentEdge": ("IssueCommentEdge", (node, None))}
