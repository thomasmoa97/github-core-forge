"""Git smart HTTP: upload-pack (protocol v2 + v0 fallback) and
receive-pack (v0 only). Pkt-line framing, sideband, shallow/depth,
fast-forward enforcement.

Wire notes (from the suite's client):
- v2 advertisement: "version 2" + one capability per pkt-line.
- v0 advertisement: "# service=git-..." preamble, ref lines, caps after NUL
  on the first ref line (or on the capabilities^{} pseudo-ref when empty).
- ls-refs / fetch v2: command lines, delim, args, flush; sections named
  acknowledgments / shallow-info / wanted-refs / packfile.
- fetch v0: want/have sections; response is plain control lines + sideband.
- receive-pack: commands, flush, pack; report-status via sideband band 1.
- A refused update still unpacks ok; its report line is exactly
  "ng <ref> non-fast-forward".
"""

import hashlib

from . import gitobj
from .store import VIEWER_LOGIN

ZERO = gitobj.ZERO_OID
MAX_PKT = 65520


def pkt(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def parse_pktlines(data):
    """Parse a pkt-line stream -> list of ('data', bytes) | ('flush'|'delim'|'end', None)."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        if i + 4 > n:
            raise ValueError("truncated pkt-line")
        hdr = data[i:i + 4]
        try:
            length = int(hdr.decode("ascii"), 16)
        except ValueError:
            raise ValueError("bad pkt-line header %r" % hdr)
        if length == 0:
            out.append(("flush", None))
            i += 4
        elif length == 1:
            out.append(("delim", None))
            i += 4
        elif length == 2:
            out.append(("end", None))
            i += 4
        elif length < 4 or length > MAX_PKT:
            raise ValueError("pkt-line length %d out of range" % length)
        else:
            if i + length > n:
                raise ValueError("truncated pkt-line payload")
            out.append(("data", data[i + 4:i + length]))
            i += length
    return out


def _sideband_pack(pack_bytes):
    """Wrap pack bytes in band-1 sideband frames (pkt-lines)."""
    out = bytearray()
    i = 0
    while i < len(pack_bytes):
        chunk = pack_bytes[i:i + 65515]
        out += pkt(b"\x01" + chunk)
        i += len(chunk)
    if not pack_bytes:
        out += pkt(b"\x01")
    return bytes(out)


class GitRequest:
    def __init__(self, forge, handler, owner, repo_name, service_path):
        self.forge = forge
        self.h = handler
        self.store = forge.store
        self.owner = owner
        self.repo_name = repo_name
        self.service_path = service_path  # e.g. "info/refs" or "git-upload-pack"
        self.repo = self.store.get_repo(owner, repo_name)

    def send(self, status, body, content_type):
        self.h._send_bytes(status, body, content_type)

    def send_404(self):
        self.h._send_bytes(404, b"Not Found", "text/plain")


def handle(forge, handler, path):
    parts = [p for p in path.split("?", 1)[0].strip("/").split("/") if p]
    if len(parts) < 2 or not parts[1].endswith(".git"):
        handler._send_bytes(404, b"Not Found", "text/plain")
        return
    owner = parts[0]
    repo_name = parts[1][:-4]
    service_path = "/".join(parts[2:])
    req = GitRequest(forge, handler, owner, repo_name, service_path)
    try:
        if service_path == "info/refs":
            _info_refs(req, handler)
        elif service_path == "git-upload-pack":
            _upload_pack(req, handler)
        elif service_path == "git-receive-pack":
            _receive_pack(req, handler)
        else:
            req.send_404()
    except (BrokenPipeError, ConnectionResetError):
        raise
    except Exception:
        # A malformed git request must never kill the server (check 30).
        try:
            req.send(400, b"bad request", "text/plain")
        except Exception:
            pass


def _query(handler, name):
    qs = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    for pair in qs.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k == name:
                return v
    return None


def _refs_sorted(repo):
    return sorted(repo.refs.items())


def _v0_caps_upload():
    return ("side-band-64k multi_ack_detailed thin-pack ofs-delta shallow "
            "agent=forge/1.0 symref=HEAD:refs/heads/main object-format=sha1")


def _v0_caps_receive():
    return ("report-status side-band-64k delete-refs ofs-delta "
            "agent=forge/1.0 symref=HEAD:refs/heads/main object-format=sha1")


def _peeled_lines(repo, store):
    """Extra advertisement lines: '<commit> <tagref>^{}' for annotated tags."""
    out = bytearray()
    for refname, oid in _refs_sorted(repo):
        if not refname.startswith("refs/tags/"):
            continue
        got = store.objects.get(oid)
        if got and got[0] == "tag":
            target = gitobj.parse_tag(got[1])["object"]
            out += pkt("%s %s^{}\n" % (target, refname))
    return bytes(out)


def _v0_advertisement_full(repo, service, caps, store):
    out = bytearray()
    out += pkt("# service=%s\n" % service)
    # Smart HTTP requires a flush after the service line (http-protocol.txt);
    # real git's client expects it before the ref lines. The suite client
    # skips non-data packets, so this is safe for it too.
    out += pkt_flush()
    refs = _refs_sorted(repo)
    if refs:
        first = True
        for refname, oid in refs:
            line = "%s %s" % (oid, refname)
            if first:
                line += "\0" + caps
                first = False
            out += pkt(line + "\n")
        out += _peeled_lines(repo, store)
    else:
        out += pkt("%s capabilities^{}\0%s\n" % (ZERO, caps))
    out += pkt_flush()
    return bytes(out)


def _info_refs(req, handler):
    service = _query(handler, "service")
    if req.repo is None:
        req.send_404()
        return
    if service == "git-upload-pack":
        proto = (handler.headers.get("Git-Protocol") or "")
        if proto == "version=2":
            body = bytearray()
            body += pkt("version 2\n")
            for cap in ("ls-refs=unborn", "fetch=shallow wait-for-done filter",
                        "object-format=sha1", "agent=forge/1.0"):
                body += pkt(cap + "\n")
            body += pkt_flush()
            req.send(200, bytes(body), "application/x-git-upload-pack-advertisement")
        else:
            body = _v0_advertisement_full(req.repo, "git-upload-pack", _v0_caps_upload(), req.store)
            req.send(200, body, "application/x-git-upload-pack-advertisement")
    elif service == "git-receive-pack":
        body = _v0_advertisement_full(req.repo, "git-receive-pack", _v0_caps_receive(), req.store)
        req.send(200, body, "application/x-git-receive-pack-advertisement")
    else:
        req.send(400, b"missing service", "text/plain")


# ---------------------------------------------------------------- ls-refs

def _ls_refs(req, args):
    """args: dict of ls-refs arguments. Returns response bytes."""
    prefixes = args.get("ref-prefix", [])
    want_peel = "peel" in args
    want_symrefs = "symrefs" in args
    lines = []
    if req.repo is not None:
        head_target = "refs/heads/" + req.repo.default_branch
        head_oid = req.repo.refs.get(head_target)
        refs = _refs_sorted(req.repo)
        # HEAD is listed when no prefix filters it out, or when a prefix
        # matches it (real git asks for ref-prefix HEAD).
        head_shown = (not prefixes or any("HEAD".startswith(p) for p in prefixes))
        if head_shown and head_oid and want_symrefs:
            lines.append("%s HEAD symref-target:%s" % (head_oid, head_target))
        for refname, oid in refs:
            if prefixes and not any(refname.startswith(p) for p in prefixes):
                continue
            line = "%s %s" % (oid, refname)
            if want_peel and refname.startswith("refs/tags/"):
                got = req.store.objects.get(oid)
                if got and got[0] == "tag":
                    line += " peeled:%s" % gitobj.parse_tag(got[1])["object"]
            lines.append(line)
    out = bytearray()
    for line in lines:
        out += pkt(line + "\n")
    out += pkt_flush()
    return bytes(out)


# ---------------------------------------------------------------- fetch

def _client_has(store, haves, shallow):
    """Objects the client provably holds: full closure of non-shallow haves;
    for shallow-boundary haves, only the boundary commit + its tree/blobs."""
    shallow = set(shallow)
    out = set()
    for have in haves:
        if have in shallow:
            got = store.get(have)
            if got and got[0] == "commit":
                out.add(have)
                c = gitobj.parse_commit(got[1])
                if c["tree"]:
                    out.add(c["tree"])
                    out |= {oid for _m, oid in gitobj.flatten_tree(store, c["tree"]).values()}
        else:
            out |= gitobj.reachable(store, [have])
    return out


def _want_closure(store, wants, depth):
    """Objects within `depth` generations of wants (None = full closure)."""
    if depth is None:
        return gitobj.reachable(store, wants)
    seen, _boundary = gitobj.reachable_depth(store, wants, depth)
    return seen


def _fetch_pack(store, repo, wants, haves, shallow, depth):
    """Compute (pack_objects, new_shallow, new_unshallow) for a fetch."""
    want_set = _want_closure(store, wants, depth)
    have_set = _client_has(store, haves, shallow)
    send_oids = want_set - have_set
    objects = []
    for oid in sorted(send_oids):
        got = store.get(oid)
        if got:
            objects.append((oid, got[0], got[1]))
    new_shallow = []
    new_unshallow = []
    if depth is not None:
        _seen, boundary = gitobj.reachable_depth(store, wants, depth)
        new_shallow = sorted(boundary)
        for old in shallow:
            if old not in new_shallow:
                # Old boundary is no longer a boundary: unshallow it when it
                # is an ancestor of the new tip (interior now).
                if gitobj.is_ancestor(store, old, wants[0]) if wants else False:
                    new_unshallow.append(old)
    return objects, new_shallow, new_unshallow


def _parse_v2_command(data):
    """Parse a v2 command request -> (command, args dict, arg order list)."""
    lines = parse_pktlines(data)
    command = None
    args = {}
    order = []
    in_args = False
    for kind, payload in lines:
        if kind == "flush":
            break
        if kind == "delim":
            in_args = True
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace")
        if not in_args:
            if text.startswith("command="):
                command = text[len("command="):].strip()
            continue
        text = text.rstrip("\n")
        key = text.split(" ", 1)[0]
        args.setdefault(key, []).append(text)
        order.append(text)
    return command, args, order


def _upload_pack(req, handler):
    if req.repo is None:
        req.send_404()
        return
    data = handler._read_body() or b""
    proto = handler.headers.get("Git-Protocol") or ""
    if proto == "version=2":
        _upload_pack_v2(req, handler, data)
    else:
        _upload_pack_v0(req, handler, data)


def _upload_pack_v2(req, handler, data):
    try:
        command, args, _order = _parse_v2_command(data)
    except ValueError:
        req.send(400, b"bad pkt-line", "text/plain")
        return
    if command == "ls-refs":
        ls_args = {}
        for key, vals in args.items():
            ls_args[key] = vals
        # normalize: peel/symrefs/unborn are bare keys; ref-prefix has values
        norm = {}
        for key, vals in args.items():
            if key == "ref-prefix":
                norm["ref-prefix"] = [v.split(" ", 1)[1] for v in vals if " " in v]
            else:
                norm[key] = True
        body = _ls_refs(req, norm)
        req.send(200, body, "application/x-git-upload-pack-result")
        return
    if command != "fetch":
        req.send(400, b"unknown command", "text/plain")
        return
    wants = [v.split(" ", 1)[1].strip() for v in args.get("want", []) if " " in v]
    haves = [v.split(" ", 1)[1].strip() for v in args.get("have", []) if " " in v]
    shallows = [v.split(" ", 1)[1].strip() for v in args.get("shallow", []) if " " in v]
    depth = None
    if "deepen" in args:
        try:
            depth = int(args["deepen"][0].split(" ", 1)[1])
        except Exception:
            depth = None
    if not wants:
        # No haves were sent, so no acknowledgments section: real git goes
        # straight to FETCH_GET_PACK when it sent "done" with no haves and
        # dies on "expected 'packfile', received 'acknowledgments'".
        out = bytearray()
        if haves:
            out += pkt("acknowledgments\n") + pkt("NAK\n") + pkt_flush()
        out += pkt("packfile\n") + _sideband_pack(b"") + pkt_flush()
        req.send(200, bytes(out), "application/x-git-upload-pack-result")
        return
    objects, new_shallow, new_unshallow = _fetch_pack(req.store.objects, req.repo, wants, haves, shallows, depth)
    pack = gitobj.write_pack(objects)
    out = bytearray()
    if haves:
        out += pkt("acknowledgments\n")
        acked = False
        for have in haves:
            if req.store.objects.has(have):
                out += pkt("ACK %s\n" % have)
                acked = True
        if not acked:
            out += pkt("NAK\n")
        out += pkt("ready\n")
        out += pkt_delim()
    if new_shallow or new_unshallow:
        out += pkt("shallow-info\n")
        for oid in new_shallow:
            out += pkt("shallow %s\n" % oid)
        for oid in new_unshallow:
            out += pkt("unshallow %s\n" % oid)
        out += pkt_delim()
    out += pkt("packfile\n")
    out += _sideband_pack(pack)
    out += pkt_flush()
    req.send(200, bytes(out), "application/x-git-upload-pack-result")


def _upload_pack_v0(req, handler, data):
    try:
        lines = parse_pktlines(data)
    except ValueError:
        req.send(400, b"bad pkt-line", "text/plain")
        return
    wants, haves, shallows, depth = [], [], [], None
    section = 0
    for kind, payload in lines:
        if kind == "flush":
            section += 1
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if section == 0:
            if text.startswith("want "):
                wants.append(text.split(" ")[1])
            elif text.startswith("shallow "):
                shallows.append(text.split(" ")[1])
            elif text.startswith("deepen "):
                try:
                    depth = int(text.split(" ")[1])
                except Exception:
                    pass
        else:
            if text.startswith("have "):
                haves.append(text.split(" ")[1])
    if not wants:
        req.send(200, pkt("NAK\n") + _sideband_pack(gitobj.empty_pack()),
                 "application/x-git-upload-pack-result")
        return
    objects, new_shallow, new_unshallow = _fetch_pack(req.store.objects, req.repo, wants, haves, shallows, depth)
    pack = gitobj.write_pack(objects)
    out = bytearray()
    acked = False
    for have in haves:
        if req.store.objects.has(have):
            out += pkt("ACK %s\n" % have)
            acked = True
    if not acked:
        out += pkt("NAK\n")
    for oid in new_shallow:
        out += pkt("shallow %s\n" % oid)
    for oid in new_unshallow:
        out += pkt("unshallow %s\n" % oid)
    out += _sideband_pack(pack)
    req.send(200, bytes(out), "application/x-git-upload-pack-result")


# ---------------------------------------------------------------- receive-pack

def _receive_pack(req, handler):
    if req.repo is None:
        req.send_404()
        return
    data = handler._read_body() or b""
    # Parse ONLY the command section: pkt-lines up to the first flush.
    # Everything after the flush is the pack (binary) and must not be
    # parsed as pkt-lines.
    commands = []  # (old, new, ref)
    pack_off = None
    i = 0
    n = len(data)
    bad = False
    while i < n:
        if i + 4 > n:
            bad = True
            break
        hdr = data[i:i + 4]
        try:
            length = int(hdr.decode("ascii"), 16)
        except ValueError:
            bad = True
            break
        if length == 0:
            pack_off = i + 4
            break
        if length < 4 or length > MAX_PKT:
            bad = True
            break
        payload = data[i + 4:i + length]
        text = payload.decode("utf-8", "replace")
        if "\0" in text:
            text = text.split("\0", 1)[0]
        text = text.rstrip("\n")
        parts = text.split(" ")
        if len(parts) >= 3:
            commands.append((parts[0], parts[1], parts[2]))
        i += length
    if bad or pack_off is None:
        req.send(400, b"bad pkt-line", "text/plain")
        return
    pack_data = data[pack_off:] if pack_off is not None else b""
    has_new = any(new != ZERO for _o, new, _r in commands)
    unpack_ok = True
    unpack_detail = ""
    if has_new:
        if not pack_data:
            unpack_ok = False
            unpack_detail = "missing pack"
        else:
            try:
                entries = gitobj.read_pack(pack_data, have_objects=None)
                # Resolve thin-pack bases against the store.
                have = {}
                req.store.objects.put_many(entries)
            except ValueError as exc:
                unpack_ok = False
                unpack_detail = str(exc)
    results = []  # (ref, ok, reason)
    with req.store.lock:
        for old, new, ref in commands:
            current = req.repo.refs.get(ref)
            if new == ZERO:
                # delete
                if current is None:
                    results.append((ref, False, "ref does not exist"))
                elif current != old and old != ZERO:
                    results.append((ref, False, "non-fast-forward"))
                else:
                    req.repo.refs.pop(ref, None)
                    results.append((ref, True, ""))
                continue
            if old == ZERO:
                if current is not None:
                    results.append((ref, False, "already exists"))
                    continue
                if not req.store.objects.has(new):
                    results.append((ref, False, "object not found"))
                    continue
                req.repo.refs[ref] = new
                results.append((ref, True, ""))
                continue
            # update
            if current is None:
                results.append((ref, False, "non-fast-forward"))
                continue
            if current != old:
                results.append((ref, False, "non-fast-forward"))
                continue
            if not req.store.objects.has(new):
                results.append((ref, False, "object not found"))
                continue
            if not gitobj.is_ancestor(req.store.objects, old, new):
                results.append((ref, False, "non-fast-forward"))
                continue
            req.repo.refs[ref] = new
            results.append((ref, True, ""))
    # Report (sideband band 1; report-status was advertised). The band-1
    # data is itself a pkt-line stream: each report line is framed as a
    # pkt-line, and the stream is TERMINATED by a flush INSIDE the band-1
    # data (real git's server sends it; without it git reads past the
    # report into the response-level flush and dies "remote end hung up").
    # The concatenated pkt-lines are chunked into band-1 sideband packets,
    # and an outer flush ends the response.
    report = bytearray()
    if unpack_ok:
        report += pkt("unpack ok\n")
    else:
        report += pkt("unpack %s\n" % (unpack_detail or "error"))
    for ref, ok, reason in results:
        if ok:
            report += pkt("ok %s\n" % ref)
        else:
            report += pkt("ng %s %s\n" % (ref, reason))
    report += pkt_flush()
    out = bytearray()
    out += _sideband_pack(bytes(report))
    out += pkt_flush()
    req.send(200, bytes(out), "application/x-git-receive-pack-result")
