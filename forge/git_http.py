"""Git smart HTTP: protocol v2 upload-pack, v0 upload-pack, v0 receive-pack.

Implements exactly what the suite's git client speaks (see the kit's
sdk/conformance/git-client.ts): v2 advertisements, ls-refs with peel /
symrefs / unborn / ref-prefix, fetch with thin-pack + ofs-delta + shallow,
side-band-64k pack framing, and report-status pushes with per-ref ok/ng.
"""
from . import gitobj
from .gitobj import ZERO, OBJ_COMMIT, OBJ_TAG

AGENT = "forge/1.0"


def _sorted_refs(repo):
    return sorted(repo.refs.items())


def _head_oid(repo):
    tip = repo.refs.get("refs/heads/" + repo.default_branch)
    return tip if tip else ZERO


def _is_annotated_tag(repo, sha):
    otype, _raw = repo.objects.get(sha, (None, None))
    return otype == OBJ_TAG


def _peeled(repo, sha):
    otype, raw = repo.objects.get(sha, (None, None))
    if otype != OBJ_TAG:
        return None
    for line in raw.split(b"\n"):
        if line.startswith(b"object "):
            return line[7:].decode()
    return None


# ---------------------------------------------------------------- advertisements

def advertisement_v2_upload_pack():
    return _ADV_V2_UPLOAD_PACK


def _build_advertisement_v2_upload_pack():
    lines = [
        b"# service=git-upload-pack\n",
        None,
        b"version 2\n",
        b"ls-refs=unborn\n",
        b"fetch=shallow wait-for-done\n",
        b"object-format=sha1\n",
        b"agent=" + AGENT.encode() + b"\n",
        None,
    ]
    out = bytearray()
    for item in lines:
        if item is None:
            out += gitobj.FLUSH
        else:
            out += gitobj.pkt_line(item)
    return bytes(out)


_ADV_V2_UPLOAD_PACK = _build_advertisement_v2_upload_pack()


def _v0_caps(service, repo):
    caps = []
    if service == "git-upload-pack":
        caps = ["side-band-64k", "side-band", "ofs-delta", "thin-pack", "shallow",
                "agent=" + AGENT, "symref=HEAD:refs/heads/" + repo.default_branch]
    else:
        caps = ["report-status", "side-band-64k", "delete-refs", "ofs-delta",
                "agent=" + AGENT, "symref=HEAD:refs/heads/" + repo.default_branch]
    return " ".join(caps)


def advertisement_v0(repo, service):
    caps = _v0_caps(service, repo)
    lines = [b"# service=" + service.encode() + b"\n", None]
    refs = _sorted_refs(repo)
    if not refs:
        lines.append(("%s capabilities^{}\0%s\n" % (ZERO, caps)).encode())
    else:
        head = _head_oid(repo)
        first = True
        lines.append(("%s HEAD\n" % head).encode())
        # capabilities ride on the first ref line
        lines[-1] = ("%s HEAD\0%s\n" % (head, caps)).encode()
        first = False
        for name, oid in refs:
            lines.append(("%s %s\n" % (oid, name)).encode())
            peeled = _peeled(repo, oid)
            if peeled:
                lines.append(("%s %s^{}\n" % (peeled, name)).encode())
        _ = first
    out = bytearray()
    for item in lines:
        if item is None:
            out += gitobj.FLUSH
        else:
            out += gitobj.pkt_line(item)
    out += gitobj.FLUSH
    return bytes(out)


# ---------------------------------------------------------------- v2 ls-refs

def ls_refs_v2(repo, args):
    peel = "peel" in args
    symrefs = "symrefs" in args
    unborn = "unborn" in args
    prefixes = [a[11:] for a in args if a.startswith("ref-prefix ")]
    refs = _sorted_refs(repo)
    out = bytearray()
    if not refs:
        if unborn:
            line = "unborn HEAD"
            if symrefs:
                line += " symref-target:refs/heads/" + repo.default_branch
            out += gitobj.pkt_line(line + "\n")
        out += gitobj.FLUSH
        return bytes(out)
    head_oid = _head_oid(repo)
    if not prefixes or any("HEAD".startswith(p) for p in prefixes):
        line = "%s HEAD" % head_oid
        if symrefs:
            line += " symref-target:refs/heads/" + repo.default_branch
        out += gitobj.pkt_line(line + "\n")
    for name, oid in refs:
        if prefixes and not any(name.startswith(p) for p in prefixes):
            continue
        line = "%s %s" % (oid, name)
        if peel:
            peeled = _peeled(repo, oid)
            if peeled:
                line += " peeled:" + peeled
        out += gitobj.pkt_line(line + "\n")
    out += gitobj.FLUSH
    return bytes(out)


# ---------------------------------------------------------------- fetch computation

def _ancestors(repo, tips, max_depth=None):
    """Return (reachable_objects, commit_depths) for the given tips.
    commit_depths: sha -> depth (0 for tips)."""
    seen_objs = set()
    depths = {}
    stack = [(t, 0) for t in tips]
    commits = []
    while stack:
        sha, d = stack.pop()
        if sha in depths:
            continue
        if sha not in repo.objects:
            continue
        otype, _raw = repo.objects[sha]
        if otype != OBJ_COMMIT:
            continue
        if max_depth is not None and d >= max_depth:
            continue
        depths[sha] = d
        commits.append(sha)
        c = gitobj.parse_commit(repo.objects[sha][1])
        for p in c["parents"]:
            stack.append((p, d + 1))
    return depths, commits


def compute_fetch(repo, wants, haves, depth, client_shallow):
    """Return (objects_to_send: set of shas, shallow: [shas], unshallow: [shas],
    acks: [shas], nak: bool)."""
    depths, commits = _ancestors(repo, wants, max_depth=depth)
    if depth is not None:
        # commits at the depth boundary are those with no parents included:
        included = set(commits)
        shallow = []
        for c in commits:
            cc = gitobj.parse_commit(repo.objects[c][1])
            if any(p not in included for p in cc["parents"]) or not cc["parents"]:
                shallow.append(c)
    else:
        shallow = []
    # objects reachable from wants (with depth limit) minus those from haves.
    # The client's have-set is the closure of its haves *excluding* the
    # parents of any shallow boundary it reports: a shallow clone holds the
    # boundary commit but not its history.
    want_objs = gitobj.collect_reachable(repo.objects, wants, depth=depth)
    have_objs = set()
    if haves:
        have_objs = gitobj.collect_reachable(repo.objects, haves)
        for s in client_shallow:
            entry = repo.objects.get(s)
            if entry is None or entry[0] != OBJ_COMMIT:
                continue
            for p in gitobj.parse_commit(entry[1])["parents"]:
                have_objs -= gitobj.collect_reachable(repo.objects, [p])
    send = want_objs - have_objs
    # acks: haves that are ancestors of (or equal to) a want
    ack_set = set()
    if haves:
        all_ancestors = set()
        _d, all_commits = _ancestors(repo, wants)
        all_ancestors = set(all_commits) | set(wants)
        for h in haves:
            if h in all_ancestors:
                ack_set.add(h)
    nak = bool(haves) and not ack_set
    unshallow = []
    if client_shallow:
        for s in client_shallow:
            if s in send or s in want_objs:
                if s not in shallow:
                    unshallow.append(s)
    return send, sorted(shallow), sorted(unshallow), sorted(ack_set), nak


def _pack_for(repo, shas):
    key = tuple(sorted(shas))
    pack = _PACK_CACHE.get(key)
    if pack is None:
        items = []
        for sha in key:
            otype, raw = repo.objects[sha]
            items.append((otype, raw))
        pack = gitobj.write_pack(items)
        if len(_PACK_CACHE) > 512:
            _PACK_CACHE.clear()
        _PACK_CACHE[key] = pack
    return pack


# Packs are a pure function of the object set (objects are immutable and
# content-addressed), and the clone workload fetches the identical pack
# dozens of times: memoize. Bounded, cleared wholesale when full.
_PACK_CACHE = {}


def _sideband_pack_chunks(pack, max_chunk=65515):
    # A pkt-line is at most 65520 bytes *including* its 4-byte length header
    # and the 1-byte side-band channel, so the data chunk is at most 65515.
    out = bytearray()
    i = 0
    while i < len(pack):
        chunk = pack[i:i + max_chunk]
        out += gitobj.pkt_line(b"\x01" + chunk)
        i += len(chunk)
    return bytes(out)


# ---------------------------------------------------------------- v2 upload-pack

def _parse_v2_request(body):
    """Split a v2 request into (command, args) where args are the lines after delim."""
    packets = gitobj.parse_pkt_lines(body)
    command = None
    args = []
    seen_delim = False
    for p in packets:
        if p is None:
            continue
        if p == b"0001":
            seen_delim = True
            continue
        text = p.decode("utf-8", "replace")
        if text.startswith("command="):
            command = text[len("command="):].strip()
        elif seen_delim:
            args.append(text.rstrip("\n"))
    return command, args


def handle_upload_pack_v2(repo, body):
    command, args = _parse_v2_request(body)
    if command == "ls-refs":
        return ls_refs_v2(repo, args), "application/x-git-upload-pack-result"
    if command != "fetch":
        return gitobj.pkt_lines([b"error unknown command\n"]), "application/x-git-upload-pack-result"
    wants = []
    haves = []
    depth = None
    client_shallow = []
    for a in args:
        if a.startswith("want "):
            wants.append(a[5:].split(" ")[0])
        elif a.startswith("have "):
            haves.append(a[5:].strip())
        elif a.startswith("deepen "):
            try:
                depth = int(a[7:])
            except ValueError:
                pass
        elif a.startswith("shallow "):
            client_shallow.append(a[8:].strip())
    send, shallow, unshallow, acks, nak = compute_fetch(repo, wants, haves, depth, client_shallow)
    out = bytearray()
    if haves:
        out += gitobj.pkt_line(b"acknowledgments\n")
        if nak:
            out += gitobj.pkt_line(b"NAK\n")
        else:
            for a in acks:
                out += gitobj.pkt_line(("ACK %s\n" % a).encode())
            out += gitobj.pkt_line(b"ready\n")
        out += b"0001"  # delim
    if shallow or unshallow:
        out += gitobj.pkt_line(b"shallow-info\n")
        for s in shallow:
            out += gitobj.pkt_line(("shallow %s\n" % s).encode())
        for s in unshallow:
            out += gitobj.pkt_line(("unshallow %s\n" % s).encode())
        out += b"0001"
    out += gitobj.pkt_line(b"packfile\n")
    pack = _pack_for(repo, send)
    out += _sideband_pack_chunks(pack)
    out += gitobj.FLUSH
    return bytes(out), "application/x-git-upload-pack-result"


# ---------------------------------------------------------------- v0 upload-pack

def handle_upload_pack_v0(repo, body):
    packets = gitobj.parse_pkt_lines(body)
    wants = []
    haves = []
    depth = None
    client_shallow = []
    section = 0
    for p in packets:
        if p is None:
            section += 1
            continue
        text = p.decode("utf-8", "replace").rstrip("\n")
        if text.startswith("want "):
            rest = text[5:]
            wants.append(rest.split(" ")[0])
        elif text.startswith("have "):
            haves.append(text[5:].strip())
        elif text.startswith("deepen "):
            try:
                depth = int(text[7:])
            except ValueError:
                pass
        elif text.startswith("shallow "):
            client_shallow.append(text[8:].strip())
    send, shallow, unshallow, acks, nak = compute_fetch(repo, wants, haves, depth, client_shallow)
    out = bytearray()
    if haves:
        if nak:
            out += gitobj.pkt_line(b"NAK\n")
        else:
            for a in acks:
                out += gitobj.pkt_line(("ACK %s\n" % a).encode())
    elif not acks:
        # v0 clients expect a NAK line even when they sent no haves.
        out += gitobj.pkt_line(b"NAK\n")
    for s in shallow:
        out += gitobj.pkt_line(("shallow %s\n" % s).encode())
    for s in unshallow:
        out += gitobj.pkt_line(("unshallow %s\n" % s).encode())
    pack = _pack_for(repo, send)
    out += _sideband_pack_chunks(pack)
    out += gitobj.FLUSH
    return bytes(out), "application/x-git-upload-pack-result"


# ---------------------------------------------------------------- v0 receive-pack

def _is_ancestor(repo, ancestor, tip):
    if ancestor == tip:
        return True
    if ancestor == ZERO:
        return True
    seen = set()
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha == ancestor:
            return True
        if sha in seen or sha not in repo.objects:
            continue
        seen.add(sha)
        otype, raw = repo.objects[sha]
        if otype == OBJ_COMMIT:
            stack.extend(gitobj.parse_commit(raw)["parents"])
        elif otype == OBJ_TAG:
            for line in raw.split(b"\n"):
                if line.startswith(b"object "):
                    stack.append(line[7:].decode())
                    break
    return False


def handle_receive_pack(repo, body):
    # Split command section (pkt-lines up to first flush) from the packfile.
    packets = []
    i = 0
    n = len(body)
    pack_start = n
    while i < n:
        if body[i:i + 4] == b"0000":
            pack_start = i + 4
            packets.append(None)
            break
        size = int(body[i:i + 4], 16)
        packets.append(body[i + 4:i + size])
        i += size
    commands = []
    for p in packets:
        if p is None:
            continue
        text = p.decode("utf-8", "replace")
        if "\0" in text:
            text = text.split("\0", 1)[0]
        text = text.rstrip("\n")
        parts = text.split(" ")
        if len(parts) >= 3:
            commands.append((parts[0], parts[1], parts[2]))
    pack_bytes = body[pack_start:] if pack_start < n else b""
    unpack_ok = True
    unpack_err = ""
    new_objects = {}
    if pack_bytes:
        try:
            new_objects = gitobj.read_pack(pack_bytes)
        except Exception as e:  # noqa: BLE001 - report as unpack error
            unpack_ok = False
            unpack_err = str(e)[:120]
    if unpack_ok:
        repo.objects.update(new_objects)
    results = []  # (ref, ok, reason)
    for old, new, ref in commands:
        if not unpack_ok:
            results.append((ref, False, "unpacker error"))
            continue
        current = repo.refs.get(ref)
        if new == ZERO:
            # delete
            if current is None:
                results.append((ref, False, "remote ref does not exist"))
            elif old != ZERO and old != current:
                results.append((ref, False, "non-fast-forward"))
            else:
                del repo.refs[ref]
                results.append((ref, True, ""))
            continue
        if old == ZERO:
            if current is not None:
                results.append((ref, False, "already exists"))
                continue
        else:
            if current is None:
                results.append((ref, False, "remote ref does not exist"))
                continue
            if old != current:
                results.append((ref, False, "non-fast-forward"))
                continue
        # fast-forward check for commits
        if new in repo.objects and repo.objects[new][0] == OBJ_COMMIT and old != ZERO:
            if not _is_ancestor(repo, old, new):
                results.append((ref, False, "non-fast-forward"))
                continue
        if new not in repo.objects and new != ZERO:
            results.append((ref, False, "unpacker error"))
            continue
        repo.refs[ref] = new
        results.append((ref, True, ""))
    inner = bytearray()
    if unpack_ok:
        inner += gitobj.pkt_line(b"unpack ok\n")
    else:
        inner += gitobj.pkt_line(("unpack error %s\n" % unpack_err).encode())
    for ref, ok, reason in results:
        if ok:
            inner += gitobj.pkt_line(("ok %s\n" % ref).encode())
        else:
            inner += gitobj.pkt_line(("ng %s %s\n" % (ref, reason)).encode())
    inner += gitobj.FLUSH
    out = bytearray()
    out += gitobj.pkt_line(b"\x01" + bytes(inner))
    out += gitobj.FLUSH
    return bytes(out), "application/x-git-receive-pack-result"
