"""IMAP operations: list folders, list/search/read/download, append (draft/sent)."""
from __future__ import annotations

import base64
import email
import imaplib
import re
import time
from email.header import decode_header, make_header

from . import utf7
from .accounts import Account

_LIST_RE = re.compile(
    r'^\(([^)]*)\)\s+(?:"([^"]*)"|(\S+))\s+(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$'
)


def _connect(a: Account) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(a.imap_host, a.imap_port)
    m.login(a.user, a.password)
    return m


def _decode(raw) -> str:
    if not raw:
        return ""
    return str(make_header(decode_header(raw)))


def _folder_arg(folder: str) -> str:
    enc = utf7.encode(folder)
    return f'"{enc}"' if any(c in enc for c in ' "\\') else enc


def list_folders(a: Account) -> list[dict]:
    m = _connect(a)
    try:
        _, boxes = m.list()
        out: list[dict] = []
        for b in boxes or []:
            if not b:
                continue
            line = b.decode(errors="replace")
            match = _LIST_RE.match(line)
            if not match:
                out.append({"name": line, "raw": line, "flags": ""})
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
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _header_meta(msg) -> dict:
    from email.utils import parsedate_to_datetime

    try:
        iso = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else None
    except Exception:
        iso = None
    return {
        "from": _decode(msg["From"]),
        "to": _decode(msg["To"]),
        "cc": _decode(msg["Cc"]),
        "subject": _decode(msg["Subject"]),
        "date": iso or msg["Date"],
    }


def list_recent(a: Account, folder: str = "INBOX", limit: int = 20) -> list[dict]:
    m = _connect(a)
    try:
        m.select(_folder_arg(folder))
        _, ids = m.search(None, "ALL")
        out: list[dict] = []
        for i in ids[0].split()[-limit:][::-1]:
            _, data = m.fetch(i, "(RFC822.HEADER)")
            if not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            out.append({"uid": i.decode(), **_header_meta(msg)})
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def search(
    a: Account,
    query: str,
    field: str = "SUBJECT",
    folder: str = "INBOX",
    limit: int = 20,
) -> list[dict]:
    field_up = field.upper().strip()
    if field_up not in {"FROM", "TO", "SUBJECT", "BODY", "TEXT"}:
        raise ValueError(f"invalid field {field!r}")
    m = _connect(a)
    try:
        m.select(_folder_arg(folder))
        _, ids = m.search(None, field_up, f'"{query}"')
        out: list[dict] = []
        for i in ids[0].split()[-limit:][::-1]:
            _, data = m.fetch(i, "(RFC822.HEADER)")
            if not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            out.append({"uid": i.decode(), **_header_meta(msg)})
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _walk_parts(msg) -> tuple[str, str, list[dict]]:
    body_text, body_html = "", ""
    attachments: list[dict] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            fn = part.get_filename()
            if fn or "attachment" in disp:
                attachments.append(
                    {"filename": _decode(fn) or "unnamed", "mime": ctype, "part": part}
                )
                continue
            if ctype == "text/plain" and not body_text:
                body_text = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            elif ctype == "text/html" and not body_html:
                body_html = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
    else:
        payload = msg.get_payload(decode=True) or b""
        body_text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return body_text, body_html, attachments


def read_email(
    a: Account, uid: str, folder: str = "INBOX", include_attachments: bool = False
) -> dict:
    m = _connect(a)
    try:
        m.select(_folder_arg(folder))
        _, data = m.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            raise ValueError(f"uid {uid!r} not found in {folder!r}")
        msg = email.message_from_bytes(data[0][1])
        body_text, body_html, atts = _walk_parts(msg)
        out_atts: list[dict] = []
        for a_ in atts:
            entry = {"filename": a_["filename"], "mime": a_["mime"]}
            if include_attachments:
                payload = a_["part"].get_payload(decode=True) or b""
                entry["size"] = len(payload)
                entry["content_base64"] = base64.b64encode(payload).decode()
            out_atts.append(entry)
        return {
            "uid": uid,
            **_header_meta(msg),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": out_atts,
        }
    finally:
        try:
            m.logout()
        except Exception:
            pass


def download_attachments(
    a: Account,
    uid: str,
    folder: str = "INBOX",
    filenames: list[str] | None = None,
) -> list[dict]:
    m = _connect(a)
    try:
        m.select(_folder_arg(folder))
        _, data = m.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            raise ValueError(f"uid {uid!r} not found in {folder!r}")
        msg = email.message_from_bytes(data[0][1])
        _, _, atts = _walk_parts(msg)
        out: list[dict] = []
        for a_ in atts:
            if filenames and a_["filename"] not in filenames:
                continue
            payload = a_["part"].get_payload(decode=True) or b""
            out.append(
                {
                    "filename": a_["filename"],
                    "mime": a_["mime"],
                    "size": len(payload),
                    "content_base64": base64.b64encode(payload).decode(),
                }
            )
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def append_message(a: Account, folder: str, raw: bytes, flags: str = "\\Seen") -> dict:
    m = _connect(a)
    try:
        status, resp = m.append(
            _folder_arg(folder),
            f"({flags})",
            imaplib.Time2Internaldate(time.time()),
            raw,
        )
        detail = b" ".join(x for x in (resp or []) if x).decode(errors="replace")
        if status != "OK":
            return {"ok": False, "folder": folder, "error": f"IMAP APPEND failed: {status} {detail}"}
        return {"ok": True, "folder": folder, "response": detail}
    finally:
        try:
            m.logout()
        except Exception:
            pass
