"""IMAP operations: folder + message CRUD."""
from __future__ import annotations

import base64
import email
import imaplib
import re
import time
from email.header import decode_header, make_header

from . import utf7
from .accounts import Account
from .errors import FolderNotFound, InvalidField, MessageNotFound

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


def list_recent(a: Account, folder: str = "INBOX", limit: int = 20,
                cursor: str | None = None) -> dict:
    """Return most-recent `limit` messages before `cursor` (UID).

    Response shape: {messages: [...], next_cursor: str|None}
    Pass `next_cursor` back as `cursor` to page further back in time.
    """
    m = _connect(a)
    try:
        _select(m, folder, a.name)
        _, ids = m.search(None, "ALL")
        all_ids = ids[0].split() if ids and ids[0] else []
        if cursor:
            try:
                cutoff = int(cursor)
                all_ids = [i for i in all_ids if int(i) < cutoff]
            except ValueError:
                pass
        page = all_ids[-limit:][::-1]
        out: list[dict] = []
        for i in page:
            _, data = m.fetch(i, "(RFC822.HEADER)")
            if not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            out.append({"uid": i.decode(), **_header_meta(msg)})
        oldest_shown = int(page[-1]) if page else None
        next_cursor = str(oldest_shown) if oldest_shown and any(int(i) < oldest_shown for i in all_ids) else None
        return {"messages": out, "next_cursor": next_cursor}
    finally:
        try:
            m.logout()
        except Exception:
            pass


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
    m = _connect(a)
    try:
        _select(m, folder, a.name)
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
        _select(m, folder, a.name)
        _, data = m.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            raise MessageNotFound(uid, folder)
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
        _select(m, folder, a.name)
        _, data = m.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            raise MessageNotFound(uid, folder)
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


# ── New in 0.7.0: full mailbox management ──────────────────────────────


def _select(m: imaplib.IMAP4_SSL, folder: str, account: str) -> None:
    status, _ = m.select(_folder_arg(folder))
    if status != "OK":
        raise FolderNotFound(folder, account)


def _fetch_flags(m: imaplib.IMAP4_SSL, uid: str) -> list[str]:
    _, data = m.fetch(uid.encode(), "(FLAGS)")
    if not data or not data[0]:
        return []
    # data[0] like: b'12 (FLAGS (\\Seen \\Flagged))'
    text = data[0].decode(errors="replace") if isinstance(data[0], bytes) else str(data[0])
    m2 = re.search(r"FLAGS\s+\(([^)]*)\)", text)
    if not m2:
        return []
    return m2.group(1).split()


def _msg_exists(m: imaplib.IMAP4_SSL, uid: str) -> bool:
    _, data = m.fetch(uid.encode(), "(UID)")
    return bool(data and data[0])


def set_flags(a: Account, uid: str, folder: str, add: list[str] | None = None,
              remove: list[str] | None = None) -> dict:
    m = _connect(a)
    try:
        _select(m, folder, a.name)
        if not _msg_exists(m, uid):
            raise MessageNotFound(uid, folder)
        if add:
            m.store(uid.encode(), "+FLAGS", "(" + " ".join(add) + ")")
        if remove:
            m.store(uid.encode(), "-FLAGS", "(" + " ".join(remove) + ")")
        flags_after = _fetch_flags(m, uid)
        return {"ok": True, "account": a.name, "uid": uid, "folder": folder,
                "flags_after": flags_after}
    finally:
        try: m.logout()
        except Exception: pass


def mark_read(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, add=["\\Seen"])


def mark_unread(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, remove=["\\Seen"])


def star(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, add=["\\Flagged"])


def unstar(a: Account, uid: str, folder: str = "INBOX") -> dict:
    return set_flags(a, uid, folder, remove=["\\Flagged"])


def copy_message(a: Account, uid: str, source: str, destination: str) -> dict:
    m = _connect(a)
    try:
        _select(m, source, a.name)
        if not _msg_exists(m, uid):
            raise MessageNotFound(uid, source)
        status, resp = m.copy(uid.encode(), _folder_arg(destination))
        if status != "OK":
            detail = b" ".join(x for x in (resp or []) if x).decode(errors="replace")
            if "TRYCREATE" in detail.upper():
                raise FolderNotFound(destination, a.name)
            raise RuntimeError(f"IMAP COPY failed: {status} {detail}")
        return {"ok": True, "account": a.name, "uid": uid,
                "source_folder": source, "destination_folder": destination}
    finally:
        try: m.logout()
        except Exception: pass


def move_message(a: Account, uid: str, source: str, destination: str) -> dict:
    """Prefer IMAP MOVE (RFC 6851); fall back to COPY+STORE+EXPUNGE."""
    m = _connect(a)
    try:
        _select(m, source, a.name)
        if not _msg_exists(m, uid):
            raise MessageNotFound(uid, source)
        # try MOVE first
        typ = "MOVE"
        try:
            status, resp = m.uid("MOVE", uid, _folder_arg(destination))
        except imaplib.IMAP4.error:
            status, resp = None, None
        if status != "OK":
            typ = "COPY+EXPUNGE"
            status, resp = m.copy(uid.encode(), _folder_arg(destination))
            if status != "OK":
                detail = b" ".join(x for x in (resp or []) if x).decode(errors="replace")
                if "TRYCREATE" in detail.upper():
                    raise FolderNotFound(destination, a.name)
                raise RuntimeError(f"IMAP COPY failed: {status} {detail}")
            m.store(uid.encode(), "+FLAGS", "(\\Deleted)")
            m.expunge()
        return {"ok": True, "account": a.name, "uid": uid,
                "source_folder": source, "destination_folder": destination,
                "method": typ}
    finally:
        try: m.logout()
        except Exception: pass


def delete_message(a: Account, uid: str, folder: str = "INBOX",
                   permanent: bool = False, trash_folder: str = "INBOX.Trash") -> dict:
    """Soft-delete (move to Trash) unless `permanent=True` (expunge in place)."""
    if not permanent:
        try:
            r = move_message(a, uid, folder, trash_folder)
            return {"ok": True, "account": a.name, "uid": uid,
                    "folder": folder, "permanently_deleted": False,
                    "moved_to": trash_folder}
        except FolderNotFound:
            # trash doesn't exist → fall through to hard delete
            pass
    m = _connect(a)
    try:
        _select(m, folder, a.name)
        if not _msg_exists(m, uid):
            raise MessageNotFound(uid, folder)
        m.store(uid.encode(), "+FLAGS", "(\\Deleted)")
        m.expunge()
        return {"ok": True, "account": a.name, "uid": uid,
                "folder": folder, "permanently_deleted": True}
    finally:
        try: m.logout()
        except Exception: pass


def create_folder(a: Account, folder: str) -> dict:
    m = _connect(a)
    try:
        status, resp = m.create(_folder_arg(folder))
        if status != "OK":
            detail = b" ".join(x for x in (resp or []) if x).decode(errors="replace")
            raise RuntimeError(f"IMAP CREATE failed: {status} {detail}")
        try:
            m.subscribe(_folder_arg(folder))
        except imaplib.IMAP4.error:
            pass  # subscribe is optional
        return {"ok": True, "account": a.name, "folder": folder, "action": "create"}
    finally:
        try: m.logout()
        except Exception: pass


def delete_folder(a: Account, folder: str) -> dict:
    m = _connect(a)
    try:
        try:
            m.unsubscribe(_folder_arg(folder))
        except imaplib.IMAP4.error:
            pass
        status, resp = m.delete(_folder_arg(folder))
        if status != "OK":
            detail = b" ".join(x for x in (resp or []) if x).decode(errors="replace")
            raise RuntimeError(f"IMAP DELETE failed: {status} {detail}")
        return {"ok": True, "account": a.name, "folder": folder, "action": "delete"}
    finally:
        try: m.logout()
        except Exception: pass


def rename_folder(a: Account, folder: str, new_name: str) -> dict:
    m = _connect(a)
    try:
        status, resp = m.rename(_folder_arg(folder), _folder_arg(new_name))
        if status != "OK":
            detail = b" ".join(x for x in (resp or []) if x).decode(errors="replace")
            raise RuntimeError(f"IMAP RENAME failed: {status} {detail}")
        return {"ok": True, "account": a.name, "folder": folder, "action": "rename",
                "new_name": new_name}
    finally:
        try: m.logout()
        except Exception: pass


def get_thread(a: Account, uid: str, folder: str = "INBOX", limit: int = 50) -> dict:
    """Group messages by Message-ID / References / In-Reply-To.

    Simple heuristic: for the message at UID, collect its Message-ID + References.
    Then IMAP-search the folder for anything referencing any of those IDs.
    """
    m = _connect(a)
    try:
        _select(m, folder, a.name)
        if not _msg_exists(m, uid):
            raise MessageNotFound(uid, folder)
        _, data = m.fetch(uid.encode(), "(RFC822.HEADER)")
        if not data or not data[0]:
            raise MessageNotFound(uid, folder)
        root = email.message_from_bytes(data[0][1])
        subject = _decode(root["Subject"])
        my_id = (root["Message-ID"] or "").strip()
        refs = (root["References"] or "").split() + (root["In-Reply-To"] or "").split()
        ids: set[str] = {my_id} | {r.strip() for r in refs if r.strip()}
        # search anything referring to any of the ids
        collected: dict[str, dict] = {}
        for msg_id in list(ids):
            if not msg_id:
                continue
            try:
                _, ids2 = m.search(None, "HEADER", "Message-ID", msg_id.strip("<>"))
                for i in (ids2[0].split() if ids2 and ids2[0] else []):
                    _, hd = m.fetch(i, "(RFC822.HEADER)")
                    if hd and hd[0]:
                        mm = email.message_from_bytes(hd[0][1])
                        collected[i.decode()] = {"uid": i.decode(), **_header_meta(mm)}
            except imaplib.IMAP4.error:
                continue
        collected.setdefault(uid, {"uid": uid, **_header_meta(root)})
        messages = list(collected.values())[:limit]
        return {"account": a.name, "folder": folder, "root_uid": uid,
                "subject": subject, "messages": messages}
    finally:
        try: m.logout()
        except Exception: pass
