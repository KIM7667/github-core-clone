"""Git object store: loose objects, packfile read/write, pkt-line framing.

Pure stdlib. No git binary anywhere: everything the forge serves over
smart HTTP is built from these primitives.
"""
import hashlib
import struct
import zlib

ZERO = "0" * 40

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {OBJ_COMMIT: b"commit", OBJ_TREE: b"tree", OBJ_BLOB: b"blob", OBJ_TAG: b"tag"}
NAME_TYPES = {b"commit": OBJ_COMMIT, b"tree": OBJ_TREE, b"blob": OBJ_BLOB, b"tag": OBJ_TAG}


def obj_id(otype, data):
    h = hashlib.sha1()
    h.update(b"%s %d\0" % (TYPE_NAMES[otype], len(data)))
    h.update(data)
    return h.hexdigest()


def encode_obj(otype, data):
    return zlib.compress(TYPE_NAMES[otype] + b" " + str(len(data)).encode() + b"\0" + data, 6)


def parse_commit(data):
    """Return dict with tree, parents, author, committer, message (bytes)."""
    head, _, message = data.partition(b"\n\n")
    out = {"tree": None, "parents": [], "author": b"", "committer": b"", "message": message}
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            out["tree"] = line[5:].decode()
        elif line.startswith(b"parent "):
            out["parents"].append(line[7:].decode())
        elif line.startswith(b"author "):
            out["author"] = line[7:]
        elif line.startswith(b"committer "):
            out["committer"] = line[10:]
        elif line.startswith(b"gpgsig "):
            pass
    return out


def build_commit(tree, parents, author, committer, message):
    parts = [b"tree " + tree.encode() + b"\n"]
    for p in parents:
        parts.append(b"parent " + p.encode() + b"\n")
    parts.append(b"author " + author + b"\n")
    parts.append(b"committer " + committer + b"\n")
    parts.append(b"\n")
    parts.append(message)
    return b"".join(parts)


def parse_tree(data):
    """Return list of (mode_bytes, name_bytes, sha_hex)."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp]
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul]
        sha = data[nul + 1:nul + 21].hex()
        out.append((mode, name, sha))
        i = nul + 21
    return out


def build_tree(entries):
    """entries: iterable of (mode_bytes, name_bytes, sha_hex). Sorted by git rules:
    entries sort by name, directories as if name ends with '/'."""
    def key(e):
        name = e[1]
        if e[0] == b"40000":
            name = name + b"/"
        return name
    out = bytearray()
    for mode, name, sha in sorted(entries, key=key):
        out += mode + b" " + name + b"\0" + bytes.fromhex(sha)
    return bytes(out)


# ---------------------------------------------------------------- deltas

def _read_varint(data, i):
    result = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, i


def apply_delta(base, delta):
    src_size, i = _read_varint(delta, 0)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    tgt_size, i = _read_varint(delta, i)
    out = bytearray()
    n = len(delta)
    while i < n:
        op = delta[i]
        i += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    cp_size |= delta[i] << (8 * bit)
                    i += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[i:i + op]
            i += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


# ---------------------------------------------------------------- packfiles

def _pack_obj_header(otype, size):
    out = bytearray()
    first = (otype << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)


def _parse_pack_obj_header(data, i):
    b = data[i]
    i += 1
    otype = (b >> 4) & 0x07
    size = b & 0x0F
    shift = 4
    while b & 0x80:
        b = data[i]
        i += 1
        size |= (b & 0x7F) << shift
        shift += 7
    return otype, size, i


def read_pack(data):
    """Parse a packfile. Returns dict sha -> (otype, raw_bytes)."""
    if data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    objs = {}
    order = []
    mv = memoryview(data)
    i = 12
    for _ in range(count):
        start = i
        otype, size, i = _parse_pack_obj_header(data, i)
        if otype == OBJ_OFS_DELTA:
            b = data[i]
            i += 1
            off = b & 0x7F
            while b & 0x80:
                b = data[i]
                i += 1
                off = ((off + 1) << 7) | (b & 0x7F)
            base_pos = start - off
            base_sha, base_type = order[base_pos]
            d = zlib.decompressobj()
            raw = d.decompress(mv[i:])
            consumed = (len(data) - i) - len(d.unused_data)
            i += consumed
            raw = apply_delta(objs[base_sha][1], raw)
            sha = obj_id(base_type, raw)
            objs[sha] = (base_type, raw)
            order.append((sha, base_type))
        elif otype == OBJ_REF_DELTA:
            base_sha = data[i:i + 20].hex()
            i += 20
            d = zlib.decompressobj()
            raw = d.decompress(mv[i:])
            consumed = (len(data) - i) - len(d.unused_data)
            i += consumed
            base_type, base_raw = objs[base_sha]
            raw = apply_delta(base_raw, raw)
            sha = obj_id(base_type, raw)
            objs[sha] = (base_type, raw)
            order.append((sha, base_type))
        else:
            d = zlib.decompressobj()
            raw = d.decompress(mv[i:])
            consumed = (len(data) - i) - len(d.unused_data)
            i += consumed
            if len(raw) != size:
                raise ValueError("object size mismatch")
            sha = obj_id(otype, raw)
            objs[sha] = (otype, raw)
            order.append((sha, otype))
    return objs


def write_pack(objs):
    """objs: iterable of (otype, raw_bytes). Emits a packfile with no deltas."""
    items = list(objs)
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(items))
    for otype, raw in items:
        out += _pack_obj_header(otype, len(raw))
        out += zlib.compress(raw, 6)
    digest = hashlib.sha1(bytes(out)).digest()
    out += digest
    return bytes(out)


def collect_reachable(objects, tips, depth=None):
    """Walk commits from tips; return set of shas (commits, trees, blobs, tags).
    objects: dict sha -> (otype, raw). depth: max commit depth (1 = tip only)."""
    seen = set()
    stack = [(t, 0) for t in tips]
    while stack:
        sha, d = stack.pop()
        if sha in seen or sha not in objects:
            continue
        otype, raw = objects[sha]
        if otype == OBJ_COMMIT and depth is not None and d >= depth:
            continue
        seen.add(sha)
        if otype == OBJ_COMMIT:
            c = parse_commit(raw)
            if c["tree"]:
                stack.append((c["tree"], d))
            for p in c["parents"]:
                stack.append((p, d + 1))
        elif otype == OBJ_TREE:
            for _mode, _name, child in parse_tree(raw):
                stack.append((child, d))
        elif otype == OBJ_TAG:
            for line in raw.split(b"\n"):
                if line.startswith(b"object "):
                    stack.append((line[7:].decode(), d))
                    break
    return seen


# ---------------------------------------------------------------- pkt-line

def pkt_line(data):
    if isinstance(data, str):
        data = data.encode()
    return ("%04x" % (len(data) + 4)).encode() + data


FLUSH = b"0000"


def pkt_lines(items):
    out = bytearray()
    for it in items:
        out += pkt_line(it)
    out += FLUSH
    return bytes(out)


def parse_pkt_lines(data):
    """Parse a pkt-line stream. Returns list of payloads (bytes); flush -> None marker."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        if data[i:i + 4] == b"0000":
            out.append(None)
            i += 4
            continue
        if data[i:i + 4] == b"0001":
            out.append(b"0001")
            i += 4
            continue
        size = int(data[i:i + 4], 16)
        if size == 0:
            out.append(None)
            i += 4
            continue
        if size < 4 or i + size > n:
            raise ValueError("bad pkt-line length")
        out.append(data[i + 4:i + size])
        i += size
    return out


def sideband_pack(pack, max_chunk=65520):
    """Wrap packfile bytes in side-band-64k channel 1 pkt-lines."""
    out = bytearray()
    i = 0
    while i < len(pack):
        chunk = pack[i:i + max_chunk]
        out += pkt_line(b"\x01" + chunk)
        i += len(chunk)
    out += FLUSH
    return bytes(out)


def sideband_lines(lines, channel=1):
    """Wrap text lines (each without trailing newline handling) in side-band."""
    out = bytearray()
    for line in lines:
        if isinstance(line, str):
            line = line.encode()
        out += pkt_line(bytes([channel]) + line)
    out += FLUSH
    return bytes(out)
