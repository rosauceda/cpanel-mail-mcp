"""IMAP operations: folder + message CRUD.

Every message identifier exposed to callers is a real IMAP **UID** (stable
across sessions), never a sequence number: all message commands go through
`UID SEARCH/FETCH/STORE/COPY/MOVE/EXPUNGE`. Read-only operations select the
folder with EXAMINE so they can never change flags.
"""
from __future__ import annotations

import base64
import email
import imaplib
import os
import re
import time
from contextlib import contextmanager
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Iterator

from . import utf7
from .accounts import Account
from .errors import (
    FolderNotFound,
    InvalidField,
    InvalidUid,
    MessageNotFound,
    ToolError,
    TrashNotFound,
)

_LIST_RE = re.compile(
    r'^\(([^)]*)\)\s+(?:"([^"]*)"|(\S+))\s+(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$'
)
_UID_RE = re.compile(rb"\bUID (\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS \(([^)]*)\)")
_UID_VALUE_RE = re.compile(r"^[1-9]\d{0,9}$")  # a single nz-number, no ranges

_SUMMARY_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID"
_TRASH_CANDIDATES = (
    "INBOX.Trash", "Trash", "INBOX.Deleted Items", "Deleted Items",
    "INBOX.Deleted Messages", "Deleted Messages", "[Gmail]/Trash",
    "INBOX.Papelera", "Papelera",
)


# ── connection helpers ─────────────────────────────────────────────────


def _timeout() -> float:
    try:
        return float(os.environ.get("MCP_IMAP_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _connect(a: Account) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(a.imap_host, a.imap_port, timeout=_timeout())
    try:
        m.login(a.user, a.password)
    except Exception:
        try:
            m.shutdown()
        except Exception:
            pass
        raise
    return m


@contextmanager
def _session(a: Account) -> Iterator[imaplib.IMAP4_SSL]:
    m = _connect(a)
    try:
        yield m
    finally:
        try:
            m.logout()
        except Exception:
            pass


def check_uid(uid: str | int, what: str = "uid") -> str:
    """Accept exactly one numeric UID. Rejects ranges/wildcards like `1:*`."""
    s = str(uid).strip()
    if not _UID_VALUE_RE.match(s):
        raise InvalidUid(str(uid), what)
    return s


def _quote(s: str) -> str:
    s = s.replace("\r", "").replace("\n", "")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _folder_arg(folder: str) -> str:
    return _quote(utf7.encode(folder))


def _detail(resp) -> str:
    return b" ".join(x for x in (resp or []) if isinstance(x, bytes)).decode(errors="replace")


def _select(m: imaplib.IMAP4_SSL, folder: str, account: str, readonly: bool = False) -> None:
    try:
        status, _ = m.select(_folder_arg(folder), readonly=readonly)
    except imaplib.IMAP4.error:
        status = "BAD"
    if status != "OK":
        raise FolderNotFound(folder, account)


# ── parsing helpers ────────────────────────────────────────────────────


def _decode(raw) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:
        return str(raw)


def _part_text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _header_meta(msg) -> dict:
    try:
        iso = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else None
    except Exception:
        iso = None
    return {
        "from": _decode(msg["From"]),
        "to": _decode(msg["To"]),
        "cc": _decode(msg["Cc"]),
        "subject": _decode(msg["Subject"]),
        "date": iso or (str(msg["Date"]) if msg["Date"] else None),
    }


def _parse_fetch(data) -> dict[str, dict]:
    """imaplib FETCH response → `{uid: {"body": bytes|None, "flags": [...]}}`.

    A message with a literal comes back as `(meta, literal)` followed by a
    trailer like `b')'` or `b' UID 7 FLAGS (\\Seen))'`; without a literal it
    is a plain bytes line. UID/FLAGS may sit in either the meta or trailer.
    """
    items = [d for d in (data or []) if d is not None]
    out: dict[str, dict] = {}
    for idx, item in enumerate(items):
        if isinstance(item, tuple):
            body = item[1]
            nxt = items[idx + 1] if idx + 1 < len(items) else b""
            text = item[0] + b" " + (nxt if isinstance(nxt, bytes) else b"")
        elif isinstance(item, bytes):
            if idx > 0 and isinstance(items[idx - 1], tuple):
                continue  # trailer of the previous literal, already consumed
            body, text = None, item
        else:
            continue
        um = _UID_RE.search(text)
        if not um:
            continue
        fm = _FLAGS_RE.search(text)
        out[um.group(1).decode()] = {
            "body": body,
            "flags": fm.group(1).decode(errors="replace").split() if fm else [],
        }
    return out


def _uid_search(m: imaplib.IMAP4_SSL, *criteria: str) -> list[str]:
    status, data = m.uid("SEARCH", *criteria)
    if status != "OK":
        raise RuntimeError(f"IMAP SEARCH failed: {status} {_detail(data)}")
    return [x.decode() for x in (data[0].split() if data and data[0] else [])]


def _uid_search_text(m: imaplib.IMAP4_SSL, key: str, value: str) -> list[str]:
    """SEARCH <key> <value> with the value sent as a UTF-8 literal, so accents
    and quotes work. Falls back to a quoted US-ASCII search for servers that
    reject CHARSET UTF-8."""
    try:
        m.literal = value.encode("utf-8")
        status, data = m.uid("SEARCH", "CHARSET", "UTF-8", key)
    except imaplib.IMAP4.error:
        status, data = "BAD", []
    finally:
        m.literal = None
    if status == "OK":
        return [x.decode() for x in (data[0].split() if data and data[0] else [])]
    if not value.isascii():
        raise RuntimeError(f"IMAP server rejected a UTF-8 search: {_detail(data)}")
    return _uid_search(m, key, _quote(value))


def _require_message(m: imaplib.IMAP4_SSL, uid: str, folder: str) -> None:
    if uid not in _uid_search(m, "UID", uid):
        raise MessageNotFound(uid, folder)


def _fetch_summaries(m: imaplib.IMAP4_SSL, uids: list[str]) -> list[dict]:
    if not uids:
        return []
    status, data = m.uid(
        "FETCH", ",".join(uids),
        f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({_SUMMARY_FIELDS})])",
    )
    if status != "OK":
        raise RuntimeError(f"IMAP FETCH failed: {status} {_detail(data)}")
    parsed = _parse_fetch(data)
    counts = _fetch_attachment_counts(m, uids)
    out: list[dict] = []
    for u in uids:
        p = parsed.get(u)
        if not p or p["body"] is None:
            continue
        msg = email.message_from_bytes(p["body"])
        n = counts.get(u)
        out.append({"uid": u, **_header_meta(msg), "flags": p["flags"],
                    "has_attachments": None if n is None else n > 0,
                    "attachment_count": n})
    return out


# ── BODYSTRUCTURE: attachment detection without downloading bodies ─────

_OPEN, _CLOSE = object(), object()
_SEXP_TOKEN_RE = re.compile(rb'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def _sexp_tokens(data) -> list:
    """Tokenize an imaplib FETCH reply. Literals (`{n}` + tuple payload)
    become plain strings, atom NIL becomes None."""
    out: list = []

    def lex(chunk: bytes) -> None:
        for t in _SEXP_TOKEN_RE.findall(chunk):
            if t == b"(":
                out.append(_OPEN)
            elif t == b")":
                out.append(_CLOSE)
            elif t.startswith(b'"'):
                out.append(re.sub(rb"\\(.)", rb"\1", t[1:-1]).decode("utf-8", "replace"))
            elif t.upper() == b"NIL":
                out.append(None)
            else:
                out.append(t.decode("utf-8", "replace"))

    for item in data or []:
        if isinstance(item, tuple):
            lex(re.sub(rb"\{\d+\}\s*$", b"", item[0]))
            out.append(item[1].decode("utf-8", "replace"))
        elif isinstance(item, bytes):
            lex(item)
    return out


def _sexp(tokens: list) -> list:
    root: list = []
    stack = [root]
    for t in tokens:
        if t is _OPEN:
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif t is _CLOSE:
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(t)
    return root


def _plist(x) -> dict[str, str]:
    """IMAP parameter list `("name" "x.pdf" ...)` → {"name": "x.pdf"}."""
    if not isinstance(x, list):
        return {}
    return {str(k).lower(): v for k, v in zip(x[::2], x[1::2]) if isinstance(k, str)}


def _bs_is_attachment(bs: list, top: bool) -> bool:
    """Same rule as `_walk_parts`: disposition attachment, a file name, a
    forwarded message, or a bare non-text message — except images carrying a
    Content-ID without `disposition: attachment`, which are embedded in the
    HTML (signature logos), not files."""
    typ = str(bs[0] or "").lower()
    sub = str(bs[1] or "").lower() if len(bs) > 1 else ""
    if typ == "message" and sub in ("rfc822", "global"):
        return True
    params = _plist(bs[2]) if len(bs) > 2 else {}
    cid = bs[3] if len(bs) > 3 else None
    ext = 8 if typ == "text" else 7  # extension data follows the basic fields
    disp = bs[ext + 1] if len(bs) > ext + 1 else None
    dtype = str(disp[0]).lower() if isinstance(disp, list) and disp and disp[0] else None
    dparams = _plist(disp[1]) if isinstance(disp, list) and len(disp) > 1 else {}
    named = any(k.startswith(("name", "filename")) for k in (*params, *dparams))
    if dtype == "attachment":
        return True
    if named:
        return not (cid and typ == "image")
    return top and typ != "text"


def _bs_count(bs: list, top: bool = False) -> int:
    if bs and isinstance(bs[0], list):  # multipart: child bodies, then subtype + ext
        n = 0
        for child in bs:
            if not isinstance(child, list):
                break
            n += _bs_count(child)
        return n
    return 1 if _bs_is_attachment(bs, top) else 0


def _fetch_attachment_counts(m: imaplib.IMAP4_SSL, uids: list[str]) -> dict[str, int]:
    """{uid: number of attachments}; UIDs the server didn't describe are absent."""
    try:
        status, data = m.uid("FETCH", ",".join(uids), "(UID BODYSTRUCTURE)")
    except imaplib.IMAP4.error:
        return {}
    if status != "OK":
        return {}
    out: dict[str, int] = {}
    for item in _sexp(_sexp_tokens(data)):
        if not isinstance(item, list):
            continue
        fields = {str(k).upper(): v for k, v in zip(item[::2], item[1::2]) if isinstance(k, str)}
        uid, bs = fields.get("UID"), fields.get("BODYSTRUCTURE")
        if isinstance(uid, str) and isinstance(bs, list):
            try:
                out[uid] = _bs_count(bs, top=True)
            except (IndexError, TypeError, AttributeError):
                continue
    return out


def _fetch_raw(m: imaplib.IMAP4_SSL, uid: str, folder: str) -> tuple[bytes, list[str]]:
    status, data = m.uid("FETCH", uid, "(UID FLAGS BODY.PEEK[])")
    p = _parse_fetch(data).get(uid) if status == "OK" else None
    if not p or p["body"] is None:
        raise MessageNotFound(uid, folder)
    return p["body"], p["flags"]


def _fetch_flags(m: imaplib.IMAP4_SSL, uid: str) -> list[str]:
    _, data = m.uid("FETCH", uid, "(UID FLAGS)")
    p = _parse_fetch(data).get(uid)
    return p["flags"] if p else []


def _copyuid(m: imaplib.IMAP4_SSL) -> str | None:
    """New UID in the destination, from a `[COPYUID v src dst]` response code."""
    try:
        _, d = m.response("COPYUID")
    except Exception:
        return None
    if d and d[-1]:
        parts = d[-1].split()
        if len(parts) == 3 and parts[2].isdigit():
            return parts[2].decode()
    return None


# ── folders ────────────────────────────────────────────────────────────


def _list(m: imaplib.IMAP4_SSL) -> list[dict]:
    _, boxes = m.list()
    out: list[dict] = []
    for b in boxes or []:
        if not b:
            continue
        line = b.decode(errors="replace") if isinstance(b, bytes) else str(b)
        match = _LIST_RE.match(line)
        if not match:
            out.append({"name": line, "raw": line, "delimiter": "", "flags": ""})
            continue
        flags, delim_q, delim_u, name_q, name_u = match.groups()
        raw_name = name_q if name_q is not None else name_u
        out.append(
            {
                "name": utf7.decode(raw_name),
                "raw": raw_name,
                "delimiter": delim_q if delim_q is not None else (delim_u or ""),
                "flags": flags,
            }
        )
    return out


def list_folders(a: Account) -> list[dict]:
    with _session(a) as m:
        return _list(m)


def _detect_trash(m: imaplib.IMAP4_SSL) -> str | None:
    folders = _list(m)
    for f in folders:
        if "\\trash" in f["flags"].lower().split():
            return f["name"]
    by_lower = {f["name"].lower(): f["name"] for f in folders}
    for c in _TRASH_CANDIDATES:
        if c.lower() in by_lower:
            return by_lower[c.lower()]
    return None


# ── read ───────────────────────────────────────────────────────────────


def list_recent(a: Account, folder: str = "INBOX", limit: int = 20,
                cursor: str | None = None) -> dict:
    """Return the newest `limit` messages with UID < `cursor` (newest first).

    Response shape: {messages: [...], next_cursor: str|None}
    Pass `next_cursor` back as `cursor` to page further back in time.
    """
    with _session(a) as m:
        _select(m, folder, a.name, readonly=True)
        uids = sorted(_uid_search(m, "ALL"), key=int)
        if cursor:
            cutoff = int(check_uid(cursor, "cursor"))
            uids = [u for u in uids if int(u) < cutoff]
        page = uids[-limit:][::-1]
        messages = _fetch_summaries(m, page)
        next_cursor = page[-1] if page and len(uids) > len(page) else None
        return {"messages": messages, "next_cursor": next_cursor}


_SEARCH_FIELDS = ["FROM", "TO", "SUBJECT", "BODY", "TEXT"]


def search(
    a: Account,
    query: str,
    field: str = "SUBJECT",
    folder: str = "INBOX",
    limit: int = 20,
) -> list[dict]:
    field_up = field.upper().strip()
    if field_up not in _SEARCH_FIELDS:
        raise InvalidField(field, _SEARCH_FIELDS)
    with _session(a) as m:
        _select(m, folder, a.name, readonly=True)
        uids = sorted(_uid_search_text(m, field_up, query), key=int)
        return _fetch_summaries(m, uids[-limit:][::-1])


_MESSAGE_TYPES = ("message/rfc822", "message/global")


def _leaf_parts(part):
    """Leaf MIME parts; an attached email (message/rfc822) counts as one leaf
    instead of being walked into."""
    if part.get_content_type() in _MESSAGE_TYPES:
        yield part
    elif part.is_multipart():
        for sub in part.get_payload():
            yield from _leaf_parts(sub)
    else:
        yield part


def part_bytes(part) -> bytes:
    """Decoded payload of an attachment part (an attached email → its .eml bytes)."""
    if part.get_content_type() in _MESSAGE_TYPES:
        inner = part.get_payload()
        if isinstance(inner, list) and inner:
            return inner[0].as_bytes()
    return part.get_payload(decode=True) or b""


def _walk_parts(msg) -> tuple[str, str, list[dict]]:
    """Split a message into (text body, html body, attachments).

    A part is an attachment when it has `Content-Disposition: attachment`, a
    file name, or is an attached email; a single-part non-text message is one
    too. Images embedded in the HTML by Content-ID are listed with
    `embedded=True` (image + Content-ID, not `disposition: attachment`) and
    don't count towards `has_attachments`. BODYSTRUCTURE
    detection (`_bs_is_attachment`) follows the same rule.
    """
    body_text, body_html = "", ""
    attachments: list[dict] = []
    multipart = msg.is_multipart()
    for part in _leaf_parts(msg):
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        fn = part.get_filename()
        is_message = ctype in _MESSAGE_TYPES
        if disp == "attachment" or fn or is_message or (not multipart and part.get_content_maintype() != "text"):
            if is_message and not fn:
                fn = (_decode(part.get_payload()[0]["Subject"]) or "message") + ".eml"
            attachments.append({
                "filename": _decode(fn) or "unnamed",
                "mime": ctype,
                "embedded": bool(disp != "attachment" and part["Content-ID"]
                                 and part.get_content_maintype() == "image"),
                "part": part,
            })
            continue
        if ctype == "text/plain" and not body_text:
            body_text = _part_text(part)
        elif ctype == "text/html" and not body_html:
            body_html = _part_text(part)
    return body_text, body_html, attachments


def read_email(
    a: Account, uid: str, folder: str = "INBOX", include_attachments: bool = False
) -> dict:
    uid = check_uid(uid)
    with _session(a) as m:
        _select(m, folder, a.name, readonly=True)
        raw, flags = _fetch_raw(m, uid, folder)
    msg = email.message_from_bytes(raw)
    body_text, body_html, atts = _walk_parts(msg)
    out_atts: list[dict] = []
    for a_ in atts:
        payload = part_bytes(a_["part"])
        entry = {"filename": a_["filename"], "mime": a_["mime"], "size": len(payload),
                 "embedded": a_["embedded"]}
        if include_attachments:
            entry["content_base64"] = base64.b64encode(payload).decode()
        out_atts.append(entry)
    n_files = sum(1 for x in atts if not x["embedded"])
    return {
        "has_attachments": n_files > 0,
        "attachment_count": n_files,
        "uid": uid,
        **_header_meta(msg),
        "message_id": (msg["Message-ID"] or "").strip() or None,
        "in_reply_to": (msg["In-Reply-To"] or "").strip() or None,
        "references": " ".join((msg["References"] or "").split()) or None,
        "reply_to": _decode(msg["Reply-To"]) or None,
        "flags": flags,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": out_atts,
    }


def download_attachments(
    a: Account,
    uid: str,
    folder: str = "INBOX",
    filenames: list[str] | None = None,
) -> list[dict]:
    uid = check_uid(uid)
    with _session(a) as m:
        _select(m, folder, a.name, readonly=True)
        raw, _ = _fetch_raw(m, uid, folder)
    _, _, atts = _walk_parts(email.message_from_bytes(raw))
    out: list[dict] = []
    for a_ in atts:
        if filenames and a_["filename"] not in filenames:
            continue
        payload = part_bytes(a_["part"])
        out.append(
            {
                "filename": a_["filename"],
                "mime": a_["mime"],
                "size": len(payload),
                "embedded": a_["embedded"],
                "content_base64": base64.b64encode(payload).decode(),
            }
        )
    return out


def get_thread(a: Account, uid: str, folder: str = "INBOX", limit: int = 50) -> dict:
    """Collect the conversation around `uid` in `folder`.

    Walks Message-ID / References / In-Reply-To in both directions: ancestors
    (IDs the message references) and descendants (messages that reference
    any ID already in the thread), until no new messages turn up.
    """
    uid = check_uid(uid)
    with _session(a) as m:
        _select(m, folder, a.name, readonly=True)
        _require_message(m, uid, folder)
        status, data = m.uid("FETCH", uid, "(UID BODY.PEEK[HEADER])")
        p = _parse_fetch(data).get(uid) if status == "OK" else None
        if not p or p["body"] is None:
            raise MessageNotFound(uid, folder)
        root = email.message_from_bytes(p["body"])
        subject = _decode(root["Subject"])

        def ids_of(msg) -> set[str]:
            found = set((msg["References"] or "").split()) | set((msg["In-Reply-To"] or "").split())
            if msg["Message-ID"]:
                found.add(msg["Message-ID"].strip())
            return {i for i in found if i}

        known_ids = ids_of(root)
        frontier = set(known_ids)
        thread_uids: set[str] = {uid}
        for _ in range(10):  # bounded BFS; real threads converge in a few rounds
            if not frontier or len(thread_uids) >= limit * 2:
                break
            new_uids: set[str] = set()
            for msg_id in frontier:
                q = _quote(msg_id)
                try:
                    new_uids.update(_uid_search(
                        m, "OR", "OR", "HEADER", "Message-ID", q,
                        "HEADER", "References", q, "HEADER", "In-Reply-To", q,
                    ))
                except (RuntimeError, imaplib.IMAP4.error):
                    continue
            new_uids -= thread_uids
            thread_uids |= new_uids
            frontier = set()
            if new_uids:
                status, data = m.uid(
                    "FETCH", ",".join(sorted(new_uids, key=int)),
                    "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO)])",
                )
                for info in _parse_fetch(data).values():
                    if info["body"]:
                        ids = ids_of(email.message_from_bytes(info["body"]))
                        frontier |= ids - known_ids
                        known_ids |= ids
        ordered = sorted(thread_uids, key=int)[:limit]
        messages = _fetch_summaries(m, ordered)
        return {"account": a.name, "folder": folder, "root_uid": uid,
                "subject": subject, "messages": messages}


# ── write ──────────────────────────────────────────────────────────────


def append_message(a: Account, folder: str, raw: bytes, flags: str = "\\Seen") -> dict:
    with _session(a) as m:
        status, resp = m.append(
            _folder_arg(folder),
            f"({flags})",
            imaplib.Time2Internaldate(time.time()),
            raw,
        )
        detail = _detail(resp)
        if status != "OK":
            return {"ok": False, "folder": folder, "error": f"IMAP APPEND failed: {status} {detail}"}
        return {"ok": True, "folder": folder, "response": detail}


def set_flags(a: Account, uid: str, folder: str, add: list[str] | None = None,
              remove: list[str] | None = None) -> dict:
    uid = check_uid(uid)
    with _session(a) as m:
        _select(m, folder, a.name)
        _require_message(m, uid, folder)
        if add:
            m.uid("STORE", uid, "+FLAGS.SILENT", "(" + " ".join(add) + ")")
        if remove:
            m.uid("STORE", uid, "-FLAGS.SILENT", "(" + " ".join(remove) + ")")
        return {"ok": True, "account": a.name, "uid": uid, "folder": folder,
                "flags_after": _fetch_flags(m, uid)}


def mark_read(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, add=["\\Seen"])


def mark_unread(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, remove=["\\Seen"])


def star(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, add=["\\Flagged"])


def unstar(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, remove=["\\Flagged"])


def _raise_copy_failure(verb: str, status, resp, destination: str, account: str) -> None:
    detail = _detail(resp)
    if "TRYCREATE" in detail.upper():
        raise FolderNotFound(destination, account)
    raise RuntimeError(f"IMAP {verb} failed: {status} {detail}")


def _expunge_uid(m: imaplib.IMAP4_SSL, uid: str) -> None:
    """UID EXPUNGE (UIDPLUS) removes only this message; plain EXPUNGE is the
    fallback for servers without UIDPLUS."""
    try:
        status, _ = m.uid("EXPUNGE", uid)
        if status == "OK":
            return
    except imaplib.IMAP4.error:
        pass
    m.expunge()


def _move_uid(m: imaplib.IMAP4_SSL, uid: str, destination: str, account: str) -> tuple[str, str | None]:
    """Move one message (folder already selected). Returns (method, new_uid)."""
    try:
        status, resp = m.uid("MOVE", uid, _folder_arg(destination))
    except imaplib.IMAP4.error:  # server lacks MOVE (RFC 6851)
        status, resp = None, None
    if status == "OK":
        return "MOVE", _copyuid(m)
    if status == "NO" and "TRYCREATE" in _detail(resp).upper():
        raise FolderNotFound(destination, account)
    status, resp = m.uid("COPY", uid, _folder_arg(destination))
    if status != "OK":
        _raise_copy_failure("COPY", status, resp, destination, account)
    new_uid = _copyuid(m)
    m.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
    _expunge_uid(m, uid)
    return "COPY+EXPUNGE", new_uid


def copy_message(a: Account, uid: str, source: str, destination: str) -> dict:
    uid = check_uid(uid)
    with _session(a) as m:
        _select(m, source, a.name)
        _require_message(m, uid, source)
        status, resp = m.uid("COPY", uid, _folder_arg(destination))
        if status != "OK":
            _raise_copy_failure("COPY", status, resp, destination, a.name)
        return {"ok": True, "account": a.name, "uid": uid,
                "source_folder": source, "destination_folder": destination,
                "new_uid": _copyuid(m)}


def move_message(a: Account, uid: str, source: str, destination: str) -> dict:
    """Prefer IMAP MOVE (RFC 6851); fall back to COPY+STORE+EXPUNGE."""
    uid = check_uid(uid)
    with _session(a) as m:
        _select(m, source, a.name)
        _require_message(m, uid, source)
        method, new_uid = _move_uid(m, uid, destination, a.name)
        return {"ok": True, "account": a.name, "uid": uid,
                "source_folder": source, "destination_folder": destination,
                "method": method, "new_uid": new_uid}


def delete_message(a: Account, uid: str, folder: str = "INBOX",
                   permanent: bool = False, trash_folder: str | None = None) -> dict:
    """Move to Trash, or expunge in place when `permanent=True`.

    A soft delete never degrades into a hard delete: if no Trash folder can
    be found, it raises `TrashNotFound` and the message is left untouched.
    """
    uid = check_uid(uid)
    with _session(a) as m:
        if not permanent:
            trash = trash_folder or a.trash_folder or _detect_trash(m)
            if not trash:
                raise TrashNotFound(a.name)
            if trash == folder:
                raise ToolError(
                    f"message is already in the Trash folder {trash!r}",
                    hint="Pass `permanent=true` to delete it for good.",
                    code="already_in_trash",
                )
            _select(m, folder, a.name)
            _require_message(m, uid, folder)
            _, new_uid = _move_uid(m, uid, trash, a.name)
            return {"ok": True, "account": a.name, "uid": uid, "folder": folder,
                    "permanently_deleted": False, "moved_to": trash, "new_uid": new_uid}
        _select(m, folder, a.name)
        _require_message(m, uid, folder)
        m.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
        _expunge_uid(m, uid)
        return {"ok": True, "account": a.name, "uid": uid, "folder": folder,
                "permanently_deleted": True, "moved_to": None, "new_uid": None}


def create_folder(a: Account, folder: str) -> dict:
    with _session(a) as m:
        status, resp = m.create(_folder_arg(folder))
        if status != "OK":
            raise RuntimeError(f"IMAP CREATE failed: {status} {_detail(resp)}")
        try:
            m.subscribe(_folder_arg(folder))
        except imaplib.IMAP4.error:
            pass  # subscribe is optional
        return {"ok": True, "account": a.name, "folder": folder, "action": "create"}


def delete_folder(a: Account, folder: str) -> dict:
    with _session(a) as m:
        try:
            m.unsubscribe(_folder_arg(folder))
        except imaplib.IMAP4.error:
            pass
        status, resp = m.delete(_folder_arg(folder))
        if status != "OK":
            raise RuntimeError(f"IMAP DELETE failed: {status} {_detail(resp)}")
        return {"ok": True, "account": a.name, "folder": folder, "action": "delete"}


def rename_folder(a: Account, folder: str, new_name: str) -> dict:
    with _session(a) as m:
        status, resp = m.rename(_folder_arg(folder), _folder_arg(new_name))
        if status != "OK":
            raise RuntimeError(f"IMAP RENAME failed: {status} {_detail(resp)}")
        return {"ok": True, "account": a.name, "folder": folder, "action": "rename",
                "new_name": new_name}
