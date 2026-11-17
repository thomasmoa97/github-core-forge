"""Git objects, packfiles, deltas and reachability. Pure stdlib.

Object ids are SHA-1 over "<type> <size>\\0" + payload. Packs we write are
non-delta (version 2); packs we read may carry OFS_DELTA / REF_DELTA and
thin-pack bases supplied by the caller.
"""

import hashlib
import struct
import zlib

ZERO_OID = "0" * 40

TYPE_COMMIT = 1
TYPE_TREE = 2
TYPE_BLOB = 3
TYPE_TAG = 4
TYPE_OFS_DELTA = 6
TYPE_REF_DELTA = 7
_TYPE_TO_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TO_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


def oid_of(obj_type, payload):
    h = hashlib.sha1()
    h.update(("%s %d\0" % (obj_type, len(payload))).encode("ascii"))
    h.update(payload)
    return h.hexdigest()


def parse_commit(raw):
    """Parse commit bytes -> dict(tree, parents, author, committer, message)."""
    head, _, message = raw.partition(b"\n\n")
    out = {"tree": None, "parents": [], "author": "", "committer": "", "message": message.decode("utf-8", "replace")}
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            out["tree"] = line[5:].decode("ascii")
        elif line.startswith(b"parent "):
            out["parents"].append(line[7:].decode("ascii"))
        elif line.startswith(b"author "):
            out["author"] = line[7:].decode("utf-8", "replace")
        elif line.startswith(b"committer "):
            out["committer"] = line[10:].decode("utf-8", "replace")
    return out


def build_commit(tree_oid, parents, author, committer, message):
    lines = [b"tree " + tree_oid.encode("ascii")]
    for p in parents:
        lines.append(b"parent " + p.encode("ascii"))
    lines.append(b"author " + author.encode("utf-8"))
    lines.append(b"committer " + committer.encode("utf-8"))
    return b"\n".join(lines) + b"\n\n" + message.encode("utf-8")


def parse_tree(raw):
    """Parse tree bytes -> list of (mode_str, name_str, oid_hex)."""
    out = []
    i = 0
    n = len(raw)
    while i < n:
        sp = raw.index(b" ", i)
        mode = raw[i:sp].decode("ascii")
        nul = raw.index(b"\0", sp)
        name = raw[sp + 1:nul].decode("utf-8", "replace")
        oid = raw[nul + 1:nul + 21].hex()
        out.append((mode, name, oid))
        i = nul + 21
    return out


def build_tree(entries):
    """entries: list of (mode, name, oid). Sorted git-style (dirs as name+'/')."""
    def key(e):
        name = e[1] + ("/" if e[0] == "40000" else "")
        return name.encode("utf-8")
    out = bytearray()
    for mode, name, oid in sorted(entries, key=key):
        out += mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(oid)
    return bytes(out)


def parse_tag(raw):
    head, _, message = raw.partition(b"\n\n")
    out = {"object": None, "type": None, "tag": None, "tagger": "", "message": message.decode("utf-8", "replace")}
    for line in head.split(b"\n"):
        if line.startswith(b"object "):
            out["object"] = line[7:].decode("ascii")
        elif line.startswith(b"type "):
            out["type"] = line[5:].decode("ascii")
        elif line.startswith(b"tag "):
            out["tag"] = line[4:].decode("utf-8", "replace")
        elif line.startswith(b"tagger "):
            out["tagger"] = line[7:].decode("utf-8", "replace")
    return out


def _read_varint(data, pos):
    """Pack entry size/type varint. Returns (type_code, size, new_pos)."""
    b = data[pos]
    pos += 1
    typ = (b >> 4) & 7
    size = b & 15
    shift = 4
    while b & 0x80:
        b = data[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
    return typ, size, pos


def _read_ofs_delta_offset(data, pos):
    b = data[pos]
    pos += 1
    off = b & 0x7F
    while b & 0x80:
        b = data[pos]
        pos += 1
        off = ((off + 1) << 7) | (b & 0x7F)
    return off, pos


def apply_delta(base, delta):
    """Apply a git delta to base bytes, return the result."""
    pos = 0

    def varint():
        nonlocal pos
        v = 0
        shift = 0
        while True:
            b = delta[pos]
            pos += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return v

    src_size = varint()
    dst_size = varint()
    if src_size != len(base):
        raise ValueError("delta source size mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != dst_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


def read_pack(data, have_objects=None):
    """Parse a packfile. Returns list of (oid, type, payload).

    have_objects: dict oid -> (type, payload) used to resolve thin-pack
    delta bases that are not in the pack itself.
    """
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    pos = 12
    entries = []  # (oid, type, payload) with deltas resolved in order
    by_offset = {}  # pack offset -> (oid, type, payload)
    for _ in range(count):
        start = pos
        typ, size, pos = _read_varint(data, pos)
        if typ == TYPE_OFS_DELTA:
            off, pos = _read_ofs_delta_offset(data, pos)
            base_off = start - off
            if base_off not in by_offset:
                raise ValueError("ofs-delta base not yet read")
            base_oid, base_type, base_payload = by_offset[base_off]
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            payload = apply_delta(base_payload, delta)
            oid = oid_of(base_type, payload)
            entries.append((oid, base_type, payload))
            by_offset[start] = (oid, base_type, payload)
        elif typ == TYPE_REF_DELTA:
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            base = None
            for o, t, p in entries:
                if o == base_oid:
                    base = (t, p)
                    break
            if base is None and have_objects and base_oid in have_objects:
                base = have_objects[base_oid]
            if base is None:
                raise ValueError("ref-delta base %s missing" % base_oid)
            payload = apply_delta(base[1], delta)
            oid = oid_of(base[0], payload)
            entries.append((oid, base[0], payload))
            by_offset[start] = (oid, base[0], payload)
        else:
            obj_type = _CODE_TO_TYPE.get(typ)
            if obj_type is None:
                raise ValueError("unknown pack type %d" % typ)
            decomp = zlib.decompressobj()
            payload = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            if len(payload) != size:
                raise ValueError("pack object size mismatch")
            oid = oid_of(obj_type, payload)
            entries.append((oid, obj_type, payload))
            by_offset[start] = (oid, obj_type, payload)
    trailer = data[pos:pos + 20]
    expect = hashlib.sha1(data[:pos]).digest()
    if trailer != expect:
        raise ValueError("pack trailer mismatch")
    return entries


def write_pack(objects):
    """objects: list of (oid, type, payload). Returns pack bytes (v2, no deltas)."""
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(objects))
    for oid, obj_type, payload in objects:
        code = _TYPE_TO_CODE[obj_type]
        size = len(payload)
        header = bytearray()
        b = (code << 4) | (size & 15)
        size >>= 4
        if size:
            b |= 0x80
        header.append(b)
        while size:
            b = size & 0x7F
            size >>= 7
            if size:
                b |= 0x80
            header.append(b)
        out += bytes(header) + zlib.compress(payload)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def empty_pack():
    return write_pack([])


class ObjectStore:
    """In-memory git object store."""

    def __init__(self):
        self._objs = {}

    def put(self, obj_type, payload):
        oid = oid_of(obj_type, payload)
        self._objs.setdefault(oid, (obj_type, payload))
        return oid

    def get(self, oid):
        return self._objs.get(oid)

    def has(self, oid):
        return oid in self._objs

    def put_many(self, entries):
        for oid, obj_type, payload in entries:
            self._objs.setdefault(oid, (obj_type, payload))


def reachable(store, tips):
    """All oids reachable from tip commits/tags (commits, trees, blobs, tags)."""
    seen = set()
    stack = list(tips)
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        got = store.get(oid)
        if got is None:
            continue
        seen.add(oid)
        obj_type, payload = got
        if obj_type == "commit":
            c = parse_commit(payload)
            if c["tree"]:
                stack.append(c["tree"])
            stack.extend(c["parents"])
        elif obj_type == "tree":
            for _mode, _name, child in parse_tree(payload):
                stack.append(child)
        elif obj_type == "tag":
            t = parse_tag(payload)
            if t["object"]:
                stack.append(t["object"])
    return seen


def reachable_depth(store, tips, depth):
    """Objects reachable within `depth` commit generations from tips.

    depth=1: tip commits + their trees/blobs only (no parents).
    Returns (objects_set, shallow_oids) where shallow_oids are the boundary
    commit oids (the tips at the depth limit).
    """
    seen = set()
    boundary = set()
    frontier = [(oid, 0) for oid in tips if oid != ZERO_OID]
    visited_commits = set()
    while frontier:
        oid, gen = frontier.pop()
        if oid in visited_commits:
            continue
        got = store.get(oid)
        if got is None or got[0] != "commit":
            continue
        visited_commits.add(oid)
        seen.add(oid)
        c = parse_commit(got[1])
        if c["tree"]:
            stack = [c["tree"]]
            while stack:
                t = stack.pop()
                if t in seen:
                    continue
                g = store.get(t)
                if g is None:
                    continue
                seen.add(t)
                if g[0] == "tree":
                    for _m, _n, child in parse_tree(g[1]):
                        stack.append(child)
        if gen + 1 < depth:
            for p in c["parents"]:
                frontier.append((p, gen + 1))
        else:
            boundary.add(oid)
    return seen, boundary


def is_ancestor(store, anc, desc):
    """True if anc is an ancestor of desc (or equal). Walks commit parents."""
    if anc == desc:
        return True
    seen = set()
    stack = [desc]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        got = store.get(oid)
        if got is None or got[0] != "commit":
            continue
        parents = parse_commit(got[1])["parents"]
        if anc in parents:
            return True
        stack.extend(parents)
    return False


def merge_base(store, a, b):
    """First common ancestor of commits a and b (best-effort BFS)."""
    if a == b:
        return a
    anc_a = {}
    stack = [(a, 0)]
    while stack:
        oid, d = stack.pop()
        if oid in anc_a or oid == ZERO_OID:
            continue
        anc_a[oid] = d
        got = store.get(oid)
        if got and got[0] == "commit":
            for p in parse_commit(got[1])["parents"]:
                stack.append((p, d + 1))
    best = None
    best_d = None
    stack = [(b, 0)]
    seen = set()
    while stack:
        oid, d = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        if oid in anc_a:
            tot = d + anc_a[oid]
            if best_d is None or tot < best_d:
                best, best_d = oid, tot
        got = store.get(oid)
        if got and got[0] == "commit":
            for p in parse_commit(got[1])["parents"]:
                stack.append((p, d + 1))
    return best


def flatten_tree(store, tree_oid, prefix=""):
    """Map path -> (mode, oid) for all blobs under a tree (recursive)."""
    out = {}
    got = store.get(tree_oid)
    if got is None or got[0] != "tree":
        return out
    for mode, name, oid in parse_tree(got[1]):
        path = prefix + name
        if mode == "40000":
            out.update(flatten_tree(store, oid, path + "/"))
        else:
            out[path] = (mode, oid)
    return out


def build_tree_from_files(store, files):
    """files: dict path -> (mode, blob_oid). Returns root tree oid, storing trees."""
    # Build nested dict then emit bottom-up.
    root = {}

    def insert(node, parts, mode, oid):
        if len(parts) == 1:
            node[parts[0]] = (mode, oid)
        else:
            node.setdefault(parts[0], {})
            insert(node[parts[0]], parts[1:], mode, oid)

    for path, (mode, oid) in files.items():
        insert(root, path.split("/"), mode, oid)

    def emit(node):
        entries = []
        for name, val in node.items():
            if isinstance(val, dict):
                sub = emit(val)
                entries.append(("40000", name, sub))
            else:
                entries.append((val[0], name, val[1]))
        payload = build_tree(entries)
        return store.put("tree", payload)

    return emit(root)
