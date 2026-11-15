"""Version-control helpers shared by REST, GraphQL and MCP: building trees and
commits from path maps, flattening trees, computing PR diffs and merges.
All timestamps are fixed (the trial's now), so object ids are deterministic.
"""
import difflib
from datetime import datetime, timezone

from . import gitobj
from .gitobj import OBJ_BLOB, OBJ_COMMIT, OBJ_TREE
from .store import BASE_ISO, VIEWER

try:
    _EPOCH = int(datetime.strptime(BASE_ISO, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
except ValueError:
    _EPOCH = 1735689600

IDENT = ("%s <%s> %d +0000" % (VIEWER["name"], VIEWER["email"], _EPOCH)).encode()


def flatten_tree(objects, tree_sha, prefix=""):
    """Return {path: blob_sha} for a tree (recursive)."""
    out = {}
    if tree_sha is None:
        return out
    entry = objects.get(tree_sha)
    if entry is None:
        return out
    _otype, raw = entry
    for mode, name, sha in gitobj.parse_tree(raw):
        path = prefix + name.decode("utf-8", "surrogateescape")
        if mode == b"40000":
            out.update(flatten_tree(objects, sha, path + "/"))
        else:
            out[path] = sha
    return out


def build_tree(objects, mapping):
    """mapping: {path: blob_sha}. Creates blob refs are already stored; builds
    intermediate trees bottom-up. Returns root tree sha."""
    # Build nested dict
    root = {}
    for path, sha in mapping.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = sha
    def build(node):
        entries = []
        for name, val in node.items():
            if isinstance(val, dict):
                child = build(val)
                entries.append((b"40000", name.encode("utf-8", "surrogateescape"), child))
            else:
                entries.append((b"100644", name.encode("utf-8", "surrogateescape"), val))
        data = gitobj.build_tree(entries)
        sha = gitobj.obj_id(OBJ_TREE, data)
        objects[sha] = (OBJ_TREE, data)
        return sha
    return build(root)


def empty_tree_sha(objects):
    data = gitobj.build_tree([])
    sha = gitobj.obj_id(OBJ_TREE, data)
    objects.setdefault(sha, (OBJ_TREE, data))
    return sha


def make_commit(objects, tree_sha, parents, message, ident=None):
    ident = ident or IDENT
    if isinstance(message, str):
        message = message.encode()
    # Store the message verbatim: the REST /commits view must echo exactly
    # what the caller sent (the suite compares byte-for-byte, no trailing
    # newline is added here; callers that want one include it).
    data = gitobj.build_commit(tree_sha, parents, ident, ident, message)
    sha = gitobj.obj_id(OBJ_COMMIT, data)
    objects[sha] = (OBJ_COMMIT, data)
    return sha


def commit_tree_at(objects, ref_tip):
    """Tree sha of the commit a ref points at (None when ref is unborn)."""
    if ref_tip is None:
        return None
    entry = objects.get(ref_tip)
    if entry is None:
        return None
    _otype, raw = entry
    return gitobj.parse_commit(raw)["tree"]


def tip_commit(objects, ref_tip):
    if ref_tip is None:
        return None
    entry = objects.get(ref_tip)
    if entry is None:
        return None
    return gitobj.parse_commit(entry[1])


def list_commits(objects, tip, limit=100):
    """Commits reachable from tip, newest first (pre-order DFS)."""
    out = []
    seen = set()
    stack = [tip]
    while stack and len(out) < limit:
        sha = stack.pop(0)
        if sha is None or sha in seen or sha not in objects:
            continue
        seen.add(sha)
        otype, raw = objects[sha]
        if otype != OBJ_COMMIT:
            continue
        out.append(sha)
        c = gitobj.parse_commit(raw)
        stack = c["parents"] + stack
    return out


def diff_files(objects, base_tree, head_tree):
    """Return list of {path, status, additions, deletions, changes}."""
    base = flatten_tree(objects, base_tree) if base_tree else {}
    head = flatten_tree(objects, head_tree) if head_tree else {}
    out = []
    for path in sorted(set(base) | set(head)):
        bsha = base.get(path)
        hsha = head.get(path)
        if bsha == hsha:
            continue
        if bsha is None:
            status = "added"
            old = b""
            new = objects[hsha][1]
        elif hsha is None:
            status = "removed"
            old = objects[bsha][1]
            new = b""
        else:
            status = "modified"
            old = objects[bsha][1]
            new = objects[hsha][1]
        try:
            old_lines = old.decode("utf-8", "replace").splitlines()
            new_lines = new.decode("utf-8", "replace").splitlines()
            sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
            additions = deletions = 0
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ("insert", "replace"):
                    additions += j2 - j1
                if tag in ("delete", "replace"):
                    deletions += i2 - i1
        except Exception:
            additions = 1 if new != old else 0
            deletions = 1 if new != old else 0
        out.append({"path": path, "status": status,
                    "additions": additions, "deletions": deletions,
                    "changes": additions + deletions})
    return out


def pr_conflicts(objects, pr, current_base_tip, head_tip):
    """True when merging this PR would conflict: any file it changes has a
    different blob at the current base tip than at the PR's recorded base."""
    base_tip = pr["base_sha"]
    if base_tip is None or current_base_tip is None or head_tip is None:
        return False
    base_tree = commit_tree_at(objects, base_tip)
    cur_tree = commit_tree_at(objects, current_base_tip)
    head_tree = commit_tree_at(objects, head_tip)
    if base_tree is None or cur_tree is None or head_tree is None:
        return False
    changed = diff_files(objects, base_tree, head_tree)
    base_map = flatten_tree(objects, base_tree)
    cur_map = flatten_tree(objects, cur_tree)
    for f in changed:
        if base_map.get(f["path"]) != cur_map.get(f["path"]):
            return True
    return False
