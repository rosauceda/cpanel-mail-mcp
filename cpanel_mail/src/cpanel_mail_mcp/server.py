"""cpanel-mail-mcp — MCP server exposing per-operation tools for IMAP/SMTP.

Each mailbox action is its own MCP tool (not a single dispatcher). This lets
`tools/list` show the full capability surface and lets clients render the
right shape of input/output per tool.

Transports: stdio (default) or Streamable HTTP with bearer + optional
Cloudflare Access OIDC (see `serve()`).

Tools are async and run blocking IMAP/SMTP work in worker threads, so one
slow mailbox never stalls other users. In HTTP mode the caller's account is
resolved from *each* HTTP request (set by `UnifiedAuthASGI`), never from
session state.
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import html as html_lib
import json
import logging
import os
from collections import OrderedDict
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable

import anyio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BeforeValidator, Field

from . import cf_access, idempotency, imap_ops, oauth_proxy, passthrough, rate_limit, smtp_ops
from . import users as users_mod
from .accounts import Account, get_account, load_accounts
from .errors import SendGateBlocked, UnknownAccount
from .models import (
    AccountInfo,
    AttachmentMeta,
    DeleteResult,
    DownloadAttachmentsResult,
    FlagResult,
    FolderInfo,
    FolderMutationResult,
    ListAccountsResult,
    ListFoldersResult,
    ListRecentResult,
    MessageSummary,
    MoveResult,
    ReadEmailResult,
    SaveDraftResult,
    SearchResult,
    SendResult,
    ThreadResult,
)

log = logging.getLogger("cpanel_mail_mcp")

# Keys the auth middleware stores in the ASGI scope state of each request.
_STATE_ACCOUNT = "cpanel_mail_account"
_STATE_IDENTITY = "cpanel_mail_identity"


def _transport_security() -> TransportSecuritySettings | None:
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    origins = [o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    disabled = os.environ.get("MCP_DISABLE_DNS_REBINDING_PROTECTION", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not hosts and not origins and not disabled:
        if os.environ.get("MCP_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
            # Bound to a real interface (container, LAN, behind a proxy): the SDK's
            # localhost-only Host check would answer 421 to every proxied request.
            # DNS-rebinding protection guards unauthenticated localhost servers;
            # HTTP mode here always requires auth.
            return TransportSecuritySettings(enable_dns_rebinding_protection=False)
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=not disabled,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _load_env() -> None:
    p = os.environ.get("EMAIL_ENV_FILE")
    if p and Path(p).is_file():
        load_dotenv(p, override=False)
    else:
        load_dotenv(override=False)


_load_env()

mcp = FastMCP("cpanel-mail", transport_security=_transport_security())

_accounts_cache: dict[str, Account] | None = None


def _accounts() -> dict[str, Account]:
    global _accounts_cache
    if _accounts_cache is None:
        _accounts_cache = load_accounts()
    return _accounts_cache


def _request_state() -> dict | None:
    """ASGI scope state of the HTTP request behind the current tool call
    (None in stdio mode or outside a request)."""
    try:
        req = mcp.get_context().request_context.request
    except (LookupError, ValueError, AttributeError):
        return None
    scope = getattr(req, "scope", None)
    state = scope.get("state") if isinstance(scope, dict) else None
    return state if isinstance(state, dict) else None


def _pick_account(account_param: str | None) -> Account:
    state = _request_state()
    if state and state.get(_STATE_ACCOUNT) is not None:
        return state[_STATE_ACCOUNT]
    try:
        return get_account(_accounts(), account_param)
    except ValueError as e:
        available = list(_accounts().keys())
        raise UnknownAccount(account_param or "", available) from e


def _identity() -> str:
    state = _request_state()
    return (state or {}).get(_STATE_IDENTITY) or "local"


def _check_send_gate(confirm: str | None) -> None:
    code = os.environ.get("EMAIL_SEND_CONFIRMATION_CODE")
    if code and not hmac.compare_digest((confirm or "").encode(), code.encode()):
        raise SendGateBlocked()


def _begin(bucket: str, account: str | None) -> Account:
    rate_limit.limiter.check(bucket, _identity())
    return _pick_account(account)


async def _io(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking IMAP/SMTP work in a worker thread."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def _coerce_uid(v: Any) -> Any:
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


# Field must precede BeforeValidator, or pydantic drops `pattern` from the JSON schema.
Uid = Annotated[
    str,
    Field(pattern=r"^[1-9][0-9]*$", description="IMAP UID from list_recent / search_emails."),
    BeforeValidator(_coerce_uid),
]
Folder = Annotated[str, Field(description="IMAP folder name (see list_folders). Case-sensitive.")]
AccountParam = Annotated[
    str | None, Field(description="Account name (list_accounts). Omit for the default/your own.")
]
UnreadOnly = Annotated[bool, Field(description="Only messages without the \\Seen flag (unread).")]
Since = Annotated[
    str | None,
    Field(description="Only messages received at or after this: ISO date/datetime "
                      "(2026-09-24, 2026-09-24T08:00:00-07:00) or relative (30m, 2h, 1d, 1w). "
                      "Times without an offset use MCP_DEFAULT_TIMEZONE, else UTC."),
]


# ── Read-only tools ────────────────────────────────────────────────────


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True,
    }
)
async def list_accounts() -> ListAccountsResult:
    """List email accounts this caller can act on (no secrets returned).

    In multi-user mode only the caller's own account is returned; in
    single-tenant mode, all configured accounts appear.
    """
    state = _request_state()
    forced = (state or {}).get(_STATE_ACCOUNT)
    accts = [forced] if forced is not None else list(_accounts().values())
    return ListAccountsResult(
        accounts=[
            AccountInfo(
                name=a.name, user=a.user, smtp_host=a.smtp_host,
                imap_host=a.imap_host, sent_folder=a.sent_folder,
                drafts_folder=a.drafts_folder, trash_folder=a.trash_folder,
            )
            for a in accts
        ]
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
async def list_folders(account: AccountParam = None) -> ListFoldersResult:
    """List every IMAP folder (mailbox) the account can see, UTF-7 decoded.

    `flags` includes SPECIAL-USE markers like \\Sent, \\Drafts, \\Trash.
    """
    a = _begin("read", account)
    folders = await _io(imap_ops.list_folders, a)
    return ListFoldersResult(account=a.name, folders=[FolderInfo(**f) for f in folders])


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
async def list_recent(
    folder: Folder = "INBOX",
    limit: Annotated[int, Field(ge=1, le=200, description="Max messages per page.")] = 20,
    cursor: Annotated[
        str | None,
        Field(pattern=r"^[1-9][0-9]*$", description="Pass the previous response's `next_cursor` to page older."),
        BeforeValidator(_coerce_uid),
    ] = None,
    unread_only: UnreadOnly = False,
    since: Since = None,
    account: AccountParam = None,
) -> ListRecentResult:
    """List the most recent messages in a folder, newest first: headers, flags,
    and `has_attachments` / `attachment_count` (no bodies downloaded).

    Messages without the \\Seen flag are unread. Use `cursor`
    (= previous `next_cursor`) to page further back in time.
    """
    a = _begin("read", account)
    res = await _io(imap_ops.list_recent, a, folder, limit, cursor=cursor,
                    unread_only=unread_only, since=since)
    return ListRecentResult(
        account=a.name,
        folder=folder,
        messages=[MessageSummary(**m) for m in res["messages"]],
        next_cursor=res["next_cursor"],
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
async def search_emails(
    query: Annotated[str, Field(min_length=1, description="Search term (accents and quotes are fine).")],
    field: Annotated[str, Field(description="One of FROM, TO, SUBJECT, BODY, TEXT.")] = "SUBJECT",
    folder: Folder = "INBOX",
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
    unread_only: UnreadOnly = False,
    since: Since = None,
    account: AccountParam = None,
) -> SearchResult:
    """IMAP SEARCH over one field, newest first. Each result carries flags and
    `has_attachments` like `list_recent`.

    Note: IMAP SEARCH is substring, case-insensitive on most servers, and
    doesn't support boolean operators. For richer queries, chain calls.
    """
    a = _begin("read", account)
    results = await _io(imap_ops.search, a, query, field, folder, limit,
                        unread_only=unread_only, since=since)
    return SearchResult(
        account=a.name, folder=folder, field=field.upper(), query=query,
        results=[MessageSummary(**m) for m in results],
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
async def read_email(
    uid: Uid,
    folder: Folder = "INBOX",
    include_attachments: Annotated[bool, Field(description="If true, embed attachments as base64.")] = False,
    account: AccountParam = None,
) -> ReadEmailResult:
    """Fetch full headers + body of a message. Attachments listed by name, mime, size
    (`embedded=true` marks images inlined in the HTML, which aren't counted as files).

    Does not mark the message as read (use `mark_read`). Set
    `include_attachments=true` to also embed each attachment as base64
    (large attachments may push you over context).
    """
    a = _begin("read", account)
    data = await _io(imap_ops.read_email, a, uid, folder, include_attachments)
    return ReadEmailResult(**data)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
async def download_attachments(
    uid: Uid,
    folder: Folder = "INBOX",
    filenames: Annotated[list[str] | None, Field(description="Only fetch these filenames.")] = None,
    account: AccountParam = None,
) -> DownloadAttachmentsResult:
    """Return message attachments as base64. Filter by filenames to save bytes."""
    a = _begin("read", account)
    atts = await _io(imap_ops.download_attachments, a, uid, folder, filenames)
    return DownloadAttachmentsResult(
        account=a.name, uid=uid, folder=folder,
        attachments=[AttachmentMeta(**at) for at in atts],
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
async def get_thread(
    uid: Annotated[str, Field(pattern=r"^[1-9][0-9]*$", description="Any message UID in the thread."),
                   BeforeValidator(_coerce_uid)],
    folder: Folder = "INBOX",
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    account: AccountParam = None,
) -> ThreadResult:
    """Collect the conversation around a message (ancestors and replies) in
    one folder, oldest first, by Message-ID / References / In-Reply-To."""
    a = _begin("read", account)
    data = await _io(imap_ops.get_thread, a, uid, folder, limit)
    return ThreadResult(
        account=data["account"], folder=data["folder"], root_uid=data["root_uid"],
        subject=data["subject"],
        messages=[MessageSummary(**m) for m in data["messages"]],
    )


# ── Write tools (destructive) ──────────────────────────────────────────


def _save_to_sent_if_enabled(a: Account, raw: bytes, save: bool | None) -> dict | None:
    enabled = a.save_to_sent if save is None else save
    if not enabled:
        return None
    try:
        return imap_ops.append_message(a, a.sent_folder, raw, "\\Seen")
    except Exception as e:  # non-fatal: the mail already went out
        return {"ok": False, "error": str(e)}


def _deliver(a: Account, save_to_sent: bool | None, send_fn: Callable[[], dict]) -> dict:
    """Send (in a worker thread) and optionally APPEND to Sent."""
    result = send_fn()
    raw = result.pop("raw")
    saved = _save_to_sent_if_enabled(a, raw, save_to_sent)
    return {
        "ok": True,
        "account": a.name,
        "recipients": result["recipients"],
        "message_id": result.get("message_id"),
        "saved_to_sent": saved,
    }


_idem_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def _send_once(key: str | None, do_send: Callable[[], Awaitable[dict]]) -> SendResult:
    """Run `do_send` at most once per (caller, idempotency_key) within the TTL,
    including when two identical calls arrive concurrently."""
    if not key:
        return SendResult(**await do_send())
    identity = _identity()
    lk = (identity, key)
    lock = _idem_locks.setdefault(lk, asyncio.Lock())
    try:
        async with lock:
            cached = idempotency.store.get(identity, key)
            if cached:
                return SendResult(**cached, idempotent_replay=True)
            payload = await do_send()
            idempotency.store.put(identity, key, payload)
            return SendResult(**payload)
    finally:
        if not lock.locked():
            _idem_locks.pop(lk, None)


Attachments = Annotated[
    list[dict] | None,
    Field(description="[{content_base64|content|path, name?, mime?}, ...]. `path` is only "
                      "honored in local (stdio) mode or under MCP_ATTACHMENT_DIR."),
]


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,  # via idempotency_key
        "openWorldHint": True,
    }
)
async def send_email(
    to: Annotated[str, Field(min_length=1, description="Recipient(s), comma-separated.")],
    subject: Annotated[str, Field()] = "",
    text: Annotated[str | None, Field(description="Plain-text body.")] = None,
    html: Annotated[str | None, Field(description="HTML body; sent as multipart/alternative when both are set.")] = None,
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    reply_to: Annotated[str | None, Field()] = None,
    attachments: Attachments = None,
    save_to_sent: Annotated[bool | None, Field(description="Override the account's Save-to-Sent default.")] = None,
    confirm: Annotated[str | None, Field(description="Send-gate code if EMAIL_SEND_CONFIRMATION_CODE is set.")] = None,
    idempotency_key: Annotated[str | None, Field(description="Opaque key; identical (key, caller) within 5 min returns the cached result.")] = None,
    account: AccountParam = None,
) -> SendResult:
    """Send an email. Supports HTML, attachments, and Save-to-Sent.

    On retries after a client timeout, pass the same `idempotency_key` you
    used the first time to prevent duplicate delivery.
    """
    _check_send_gate(confirm)
    a = _begin("send", account)
    send_fn = functools.partial(
        smtp_ops.send, a, to, subject, text, html, cc, bcc, reply_to, attachments,
    )
    return await _send_once(idempotency_key, lambda: _io(_deliver, a, save_to_sent, send_fn))


def _reply_sync(a: Account, uid: str, folder: str, text: str | None, html: str | None,
                reply_all: bool, attachments: list[dict] | None,
                save_to_sent: bool | None) -> dict:
    orig = imap_ops.read_email(a, uid, folder, include_attachments=False)
    subject = orig["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    to = orig.get("reply_to") or orig["from"]
    cc = None
    if reply_all:
        seen = {a.user.lower()} | {addr.lower() for _, addr in getaddresses([to]) if addr}
        extra: list[str] = []
        for name, addr in getaddresses([x for x in (orig.get("to"), orig.get("cc")) if x]):
            if addr and "@" in addr and addr.lower() not in seen:
                seen.add(addr.lower())
                extra.append(formataddr((name, addr)))
        cc = ", ".join(extra) or None
    in_reply_to = orig.get("message_id")
    references = " ".join(x for x in (orig.get("references"), in_reply_to) if x) or None
    send_fn = functools.partial(
        smtp_ops.send, a, to, subject, text, html, cc, None, None, attachments,
        in_reply_to=in_reply_to, references=references,
    )
    return _deliver(a, save_to_sent, send_fn)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def reply_email(
    uid: Uid,
    text: Annotated[str | None, Field()] = None,
    html: Annotated[str | None, Field()] = None,
    folder: Folder = "INBOX",
    reply_all: Annotated[bool, Field(description="Also Cc the original To/Cc (minus yourself).")] = False,
    attachments: Attachments = None,
    save_to_sent: Annotated[bool | None, Field()] = None,
    confirm: Annotated[str | None, Field()] = None,
    idempotency_key: Annotated[str | None, Field()] = None,
    account: AccountParam = None,
) -> SendResult:
    """Reply to a message, threaded (In-Reply-To + References) with a `Re:` subject.

    Goes to the original Reply-To if present, else From. `reply_all=true`
    also copies the original To and Cc, excluding your own address.
    """
    _check_send_gate(confirm)
    a = _begin("send", account)
    return await _send_once(idempotency_key, lambda: _io(
        _reply_sync, a, uid, folder, text, html, reply_all, attachments, save_to_sent,
    ))


def _forward_sync(a: Account, uid: str, folder: str, to: str, note: str | None,
                  cc: str | None, bcc: str | None, save_to_sent: bool | None) -> dict:
    orig = imap_ops.read_email(a, uid, folder, include_attachments=True)
    subject = orig["subject"] or ""
    if not subject.lower().startswith(("fwd:", "fw:")):
        subject = f"Fwd: {subject}"
    header_block = (
        "---------- Forwarded message ----------\n"
        f"From: {orig.get('from', '')}\n"
        f"Date: {orig.get('date', '')}\n"
        f"Subject: {orig.get('subject', '')}\n"
        f"To: {orig.get('to', '')}\n\n"
    )
    note = (note or "").rstrip()
    lead = note + "\n\n" if note else ""
    body_html = None
    if orig.get("body_text") or not orig.get("body_html"):
        body_text = lead + header_block + (orig.get("body_text") or "")
    else:  # HTML-only original: forward the HTML as HTML
        body_text = lead + header_block + "(The original message is HTML; see the HTML version.)"
        body_html = (
            (f"<p>{html_lib.escape(note)}</p>" if note else "")
            + f"<pre>{html_lib.escape(header_block)}</pre>"
            + orig["body_html"]
        )
    fwd_atts = [
        {"name": att["filename"], "content_base64": att["content_base64"], "mime": att.get("mime")}
        for att in orig.get("attachments", [])
        if att.get("content_base64")
    ]
    send_fn = functools.partial(
        smtp_ops.send, a, to, subject, body_text, body_html, cc, bcc, None, fwd_atts,
    )
    return _deliver(a, save_to_sent, send_fn)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def forward_email(
    uid: Uid,
    to: Annotated[str, Field(min_length=1)],
    text: Annotated[str | None, Field(description="Additional note to include above the forwarded body.")] = None,
    folder: Folder = "INBOX",
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    save_to_sent: Annotated[bool | None, Field()] = None,
    confirm: Annotated[str | None, Field()] = None,
    idempotency_key: Annotated[str | None, Field()] = None,
    account: AccountParam = None,
) -> SendResult:
    """Forward a message with its attachments. Subject gets `Fwd:`; original headers quoted."""
    _check_send_gate(confirm)
    a = _begin("send", account)
    return await _send_once(idempotency_key, lambda: _io(
        _forward_sync, a, uid, folder, to, text, cc, bcc, save_to_sent,
    ))


def _save_draft_sync(a: Account, box: str, to: str, subject: str, text: str | None,
                     html: str | None, cc: str | None, bcc: str | None,
                     attachments: list[dict] | None) -> dict:
    msg = smtp_ops.build_message(a, to, subject, text, html, cc, bcc, None, attachments)
    return imap_ops.append_message(a, box, msg.as_bytes(), "\\Draft \\Seen")


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def save_draft(
    to: Annotated[str, Field()] = "",
    subject: Annotated[str, Field()] = "",
    text: Annotated[str | None, Field()] = None,
    html: Annotated[str | None, Field()] = None,
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    attachments: Attachments = None,
    folder: Annotated[str | None, Field(description="Override Drafts folder (default: account.drafts_folder).")] = None,
    account: AccountParam = None,
) -> SaveDraftResult:
    """Save an unsent draft in the account's Drafts folder."""
    a = _begin("send", account)
    box = folder or a.drafts_folder
    result = await _io(_save_draft_sync, a, box, to, subject, text, html, cc, bcc, attachments)
    return SaveDraftResult(
        ok=result.get("ok", False),
        account=a.name,
        folder=box,
        response=result.get("response"),
        error=result.get("error"),
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def send_invite(
    to: Annotated[str, Field(min_length=1)],
    subject: Annotated[str, Field(min_length=1)],
    start: Annotated[str, Field(description="ISO 8601 (with offset) or 'YYYY-MM-DD HH:MM'. Times without an offset use `timezone`.")],
    end: Annotated[str, Field()],
    description: Annotated[str, Field()] = "",
    location: Annotated[str, Field()] = "",
    organizer: Annotated[str | None, Field()] = None,
    attendees: Annotated[list[str] | None, Field()] = None,
    timezone: Annotated[str | None, Field(description="IANA zone for times without an offset, e.g. 'America/Mexico_City'. Default: MCP_DEFAULT_TIMEZONE, else UTC.")] = None,
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    text: Annotated[str | None, Field()] = None,
    html: Annotated[str | None, Field()] = None,
    save_to_sent: Annotated[bool | None, Field()] = None,
    confirm: Annotated[str | None, Field()] = None,
    idempotency_key: Annotated[str | None, Field()] = None,
    account: AccountParam = None,
) -> SendResult:
    """Send a calendar invite (RFC 5545 ICS, METHOD:REQUEST)."""
    _check_send_gate(confirm)
    a = _begin("send", account)
    send_fn = functools.partial(
        smtp_ops.send_invite, a, to, subject, start, end, description, location,
        organizer, attendees, cc, bcc, text, html, timezone=timezone,
    )
    return await _send_once(idempotency_key, lambda: _io(_deliver, a, save_to_sent, send_fn))


# ── Flag / move / delete ───────────────────────────────────────────────


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def mark_read(uid: Uid, folder: Folder = "INBOX", account: AccountParam = None) -> FlagResult:
    """Add the \\Seen flag."""
    a = _begin("read", account)
    return FlagResult(**await _io(imap_ops.mark_read, a, uid, folder))


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def mark_unread(uid: Uid, folder: Folder = "INBOX", account: AccountParam = None) -> FlagResult:
    """Remove the \\Seen flag."""
    a = _begin("read", account)
    return FlagResult(**await _io(imap_ops.mark_unread, a, uid, folder))


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def star_email(uid: Uid, folder: Folder = "INBOX", account: AccountParam = None) -> FlagResult:
    """Add the \\Flagged (starred) flag."""
    a = _begin("read", account)
    return FlagResult(**await _io(imap_ops.star, a, uid, folder))


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def unstar_email(uid: Uid, folder: Folder = "INBOX", account: AccountParam = None) -> FlagResult:
    """Remove the \\Flagged flag."""
    a = _begin("read", account)
    return FlagResult(**await _io(imap_ops.unstar, a, uid, folder))


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
async def move_email(
    uid: Uid,
    source_folder: Folder,
    destination_folder: Folder,
    account: AccountParam = None,
) -> MoveResult:
    """Move a message between folders (RFC 6851 MOVE with COPY+EXPUNGE fallback).

    The message gets a new UID in the destination (`new_uid` when known).
    """
    a = _begin("read", account)
    return MoveResult(**await _io(imap_ops.move_message, a, uid, source_folder, destination_folder))


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
async def copy_email(
    uid: Uid,
    source_folder: Folder,
    destination_folder: Folder,
    account: AccountParam = None,
) -> MoveResult:
    """Copy a message to another folder without removing the original."""
    a = _begin("read", account)
    return MoveResult(**await _io(imap_ops.copy_message, a, uid, source_folder, destination_folder))


@mcp.tool(
    annotations={"destructiveHint": True, "idempotentHint": False, "openWorldHint": True}
)
async def delete_email(
    uid: Uid,
    folder: Folder = "INBOX",
    permanent: Annotated[bool, Field(description="If false (default), move to Trash. If true, expunge in place — cannot be undone.")] = False,
    trash_folder: Annotated[str | None, Field(description="Trash folder override. Default: the account's trash_folder, else auto-detected (SPECIAL-USE \\Trash or common names).")] = None,
    account: AccountParam = None,
) -> DeleteResult:
    """Delete a message. Soft-delete by default (moves to Trash).

    If no Trash folder can be found, the call fails and nothing is deleted;
    it never falls back to a permanent delete on its own.
    """
    a = _begin("read", account)
    r = await _io(imap_ops.delete_message, a, uid, folder, permanent=permanent, trash_folder=trash_folder)
    return DeleteResult(**r)


# ── Folder management ──────────────────────────────────────────────────


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
async def create_folder(
    folder: Annotated[str, Field(min_length=1)],
    account: AccountParam = None,
) -> FolderMutationResult:
    """Create a new IMAP folder and subscribe to it."""
    a = _begin("read", account)
    return FolderMutationResult(**await _io(imap_ops.create_folder, a, folder))


@mcp.tool(
    annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
)
async def delete_folder(
    folder: Annotated[str, Field(min_length=1)],
    account: AccountParam = None,
) -> FolderMutationResult:
    """Delete an IMAP folder. Most servers refuse if it has subfolders."""
    a = _begin("read", account)
    return FolderMutationResult(**await _io(imap_ops.delete_folder, a, folder))


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
async def rename_folder(
    folder: Annotated[str, Field(min_length=1)],
    new_name: Annotated[str, Field(min_length=1)],
    account: AccountParam = None,
) -> FolderMutationResult:
    """Rename an IMAP folder."""
    a = _begin("read", account)
    return FolderMutationResult(**await _io(imap_ops.rename_folder, a, folder, new_name))


# ── ASGI middleware for HTTP transport ─────────────────────────────────


async def _send_json(send, status: int, payload: Any, extra_headers: list | None = None) -> None:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json")]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_text(send, status: int, text: str) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": text.encode()})


_www_authenticate_header: bytes = b'Bearer realm="mcp"'


def _set_www_authenticate(resource_url: str) -> None:
    global _www_authenticate_header
    if resource_url:
        _www_authenticate_header = (
            f'Bearer realm="mcp", '
            f'resource_metadata="{resource_url}/.well-known/oauth-protected-resource"'
        ).encode()


async def _send_401(send, detail: str = "unauthorized") -> None:
    await _send_json(send, 401, {"error": detail}, [(b"www-authenticate", _www_authenticate_header)])


async def _send_403(send, detail: str) -> None:
    await _send_json(send, 403, {"error": detail})


def _user_by_email(users: dict[str, Account], email: str) -> Account | None:
    needle = email.lower()
    for acct in users.values():
        if needle in acct.sso_emails or acct.user.lower() == needle:
            return acct
    return None


def _user_by_token(users: dict[str, Account], token: str) -> Account | None:
    """Constant-time compare against every token (small teams → cheap)."""
    tb = token.encode()
    found = None
    for t, acct in users.items():
        if hmac.compare_digest(t.encode(), tb):
            found = acct
    return found


def _is_public_path(path: str) -> bool:
    return path in ("/health", "/healthz") or path.startswith("/.well-known/") or path == "/register"


class _SessionBoundAuth:
    """Base for the auth middlewares: after a subclass has authenticated a
    request, `_dispatch` puts the caller's Account and identity in the ASGI
    scope state (where tools read them) and binds each MCP session to the
    identity that created it."""

    _MAX_SESSIONS = 10_000

    def __init__(self, app) -> None:
        self.app = app
        self._session_owner: OrderedDict[str, str] = OrderedDict()

    def _remember_session(self, sid: str, identity: str) -> None:
        self._session_owner[sid] = identity
        self._session_owner.move_to_end(sid)
        while len(self._session_owner) > self._MAX_SESSIONS:
            self._session_owner.popitem(last=False)

    async def _dispatch(self, scope, receive, send, acct: Account | None, identity: str) -> None:
        sid = ""
        for k, v in scope.get("headers", []):
            if k == b"mcp-session-id":
                sid = v.decode("latin-1")
                break
        if sid:
            owner = self._session_owner.get(sid)
            if owner is not None and owner != identity:
                log.warning("session %s… used by %s but owned by %s — rejected", sid[:8], identity, owner)
                await _send_json(send, 404, {"error": "session_not_found"})
                return

        state = scope.setdefault("state", {})
        state[_STATE_ACCOUNT] = acct
        state[_STATE_IDENTITY] = identity

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and not sid:
                for k, v in message.get("headers", []):
                    if k.lower() == b"mcp-session-id":
                        self._remember_session(v.decode("latin-1"), identity)
                        break
            await send(message)

        await self.app(scope, receive, send_wrapper)


class UnifiedAuthASGI(_SessionBoundAuth):
    """Authenticate every HTTP request via a CF Access JWT (Cf-Access-Jwt-Assertion
    or Bearer JWT), an opaque bearer from users.json, or a single-tenant token.
    """

    def __init__(
        self,
        app,
        *,
        users: users_mod.UsersStore | dict[str, Account] | None = None,
        single_token: str | None = None,
        cf_verifier: cf_access.CFAccessVerifier | None = None,
    ) -> None:
        if (users is None) == (not single_token):
            raise ValueError("UnifiedAuthASGI: pick exactly one of single_token or users")
        super().__init__(app)
        self._users_src = users
        self._expected = f"Bearer {single_token}".encode() if single_token else None
        self._cf = cf_verifier

    def _users(self) -> dict[str, Account]:
        src = self._users_src
        if src is None:
            return {}
        return src if isinstance(src, dict) else src.get()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if _is_public_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        cf_jwt_header = b""
        auth = b""
        for k, v in scope.get("headers", []):
            if k == b"cf-access-jwt-assertion":
                cf_jwt_header = v
            elif k == b"authorization":
                auth = v

        users = self._users()

        if self._cf and cf_jwt_header:
            try:
                claims = self._cf.verify(cf_jwt_header.decode(errors="replace").strip())
            except cf_access.CFAccessInvalid as e:
                await _send_401(send, f"cf-access-jwt-invalid: {e}")
                return
            return await self._dispatch_with_claims(scope, receive, send, users, claims)

        if self._cf and auth.startswith(b"Bearer "):
            token = auth[7:].decode("ascii", errors="replace").strip()
            if token.count(".") == 2:
                try:
                    claims = self._cf.verify(token)
                except cf_access.CFAccessInvalid:
                    claims = None
                if claims is not None:
                    return await self._dispatch_with_claims(scope, receive, send, users, claims)

        if self._users_src is not None and auth.startswith(b"Bearer "):
            token = auth[7:].decode("ascii", errors="replace").strip()
            acct = _user_by_token(users, token) if token else None
            if acct is not None:
                return await self._dispatch(scope, receive, send, acct, f"user:{acct.user.lower()}")

        if self._expected is not None and hmac.compare_digest(auth, self._expected):
            return await self._dispatch(scope, receive, send, None, "token")

        await _send_401(send)

    async def _dispatch_with_claims(self, scope, receive, send, users, claims: dict) -> None:
        email = cf_access.extract_email(claims)
        if not email:
            await _send_403(send, "cf-access-jwt-no-email")
            return
        acct = _user_by_email(users, email) if users else None
        if acct is None:
            await _send_403(send, f"unknown-user:{email}")
            return
        await self._dispatch(scope, receive, send, acct, f"user:{acct.user.lower()}")


class CredentialsAuthASGI(_SessionBoundAuth):
    """`MCP_AUTH_MODE=credentials`: every request carries the mailbox login
    (X-Email-User + X-Email-Password, or Basic auth). See `passthrough`."""

    def __init__(self, app, *, verifier: passthrough.CredentialVerifier,
                 api_key: str | None = None) -> None:
        super().__init__(app)
        self.verifier = verifier
        self._api_key = api_key.encode() if api_key else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or _is_public_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        challenge = [(b"www-authenticate", b'Basic realm="cpanel-mail-mcp"')]
        if self._api_key is not None:
            given = next((v for k, v in headers if k == b"x-api-key"), b"")
            if not hmac.compare_digest(given, self._api_key):
                await _send_json(send, 401, {"error": "invalid_api_key"}, challenge)
                return
        creds = passthrough.extract_credentials(headers)
        if creds is None:
            await _send_json(send, 401, {
                "error": "missing_credentials",
                "hint": "send X-Email-User and X-Email-Password headers (or Basic auth)",
            }, challenge)
            return
        try:
            acct = await anyio.to_thread.run_sync(self.verifier.check, *creds)
        except passthrough.InvalidCredentials:
            await _send_json(send, 401, {"error": "invalid_credentials"}, challenge)
            return
        except passthrough.DomainNotAllowed as e:
            await _send_json(send, 403, {"error": "domain_not_allowed", "domain": str(e)})
            return
        except passthrough.Throttled as e:
            await _send_json(send, 429, {"error": "too_many_failed_logins", "retry_after": e.retry_after},
                             [(b"retry-after", str(e.retry_after).encode())])
            return
        except passthrough.UpstreamUnavailable as e:
            log.error("mail server unreachable while verifying %s: %s", creds[0], e)
            await _send_json(send, 503, {"error": "mail_server_unreachable"})
            return
        await self._dispatch(scope, receive, send, acct, f"user:{acct.user}")


class WellKnownASGI:
    def __init__(
        self,
        app,
        *,
        resource_url: str,
        authorization_servers: list[str],
        proxy: oauth_proxy.OAuthProxy | None = None,
    ) -> None:
        self.app = app
        self.resource_url = resource_url
        self.proxy = proxy
        as_list = [resource_url] if proxy else authorization_servers
        self._prm_body = json.dumps({
            "resource": resource_url,
            "authorization_servers": as_list,
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/rosauceda/cpanel-mail-mcp",
        }).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path in ("/health", "/healthz"):
            await _send_text(send, 200, "ok")
            return
        if path == "/.well-known/oauth-protected-resource" or path.startswith(
            "/.well-known/oauth-protected-resource/"
        ):
            await _send_json(send, 200, self._prm_body)
            return
        if self.proxy is not None:
            if path in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ) or path.startswith("/.well-known/oauth-authorization-server/"):
                try:
                    body = await anyio.to_thread.run_sync(self.proxy.composed_metadata)
                except Exception as e:
                    await _send_json(send, 502, {"error": "upstream_unreachable", "detail": str(e)})
                    return
                await _send_json(send, 200, body)
                return
            if path == "/register" and method == "POST":
                try:
                    await self.proxy.handle_register(scope, receive, send)
                except Exception as e:
                    log.exception("DCR handler crashed: %s", e)
                    await _send_json(send, 500, {"error": "dcr_failed"})
                return
        await self.app(scope, receive, send)


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def create_http_app(transport: str = "streamable-http"):
    """Build the HTTP ASGI app (auth + well-known) from environment variables."""
    attach_dir = os.environ.get("MCP_ATTACHMENT_DIR", "").strip() or None
    # Remote callers must never read the server's own files (users.json holds
    # every mailbox password): `path` attachments only under MCP_ATTACHMENT_DIR.
    smtp_ops.configure_path_attachments(bool(attach_dir), attach_dir)

    auth_mode = os.environ.get("MCP_AUTH_MODE", "").strip().lower()
    if auth_mode not in ("", "tokens", "credentials"):
        raise SystemExit(f"unknown MCP_AUTH_MODE={auth_mode!r}. Use tokens|credentials.")
    allow_no_auth = _bool_env("MCP_ALLOW_NO_AUTH")

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()

    if auth_mode == "credentials":
        template = passthrough.template_from_env()
        app = CredentialsAuthASGI(
            app,
            verifier=passthrough.CredentialVerifier(template),
            api_key=os.environ.get("MCP_API_KEY", "").strip() or None,
        )
        log.info(
            "credentials mode: callers log in with their own mailbox on %s (domains: %s, api key: %s)",
            template.imap_host, ", ".join(template.allowed_domains) or "any",
            "required" if os.environ.get("MCP_API_KEY", "").strip() else "off",
        )
    elif users_mod.is_multi_user():
        users_file = users_mod.users_path()
        users_mod.load_users(users_file)  # fail fast on a malformed file
        store = users_mod.UsersStore(users_file)
        if not store.get():
            log.warning(
                "%s has no users yet — add one with `cpanel-mail-mcp admin add-user`; "
                "changes are picked up without a restart", users_file,
            )
        cf_verifier = cf_access.from_env()
        if cf_verifier:
            log.info("CF Access OIDC ENABLED: team=%s aud=%s…",
                     cf_verifier.team_domain, cf_verifier.audience[:10])
        app = UnifiedAuthASGI(app, users=store, cf_verifier=cf_verifier)
        log.info("multi-user mode: %d user(s) loaded from %s", len(store.get()), store.path)
    else:
        single_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
        if single_token:
            app = UnifiedAuthASGI(app, single_token=single_token, cf_verifier=cf_access.from_env())
            log.info("single-tenant mode: bearer auth ENABLED")
        elif allow_no_auth:
            log.warning("bearer auth DISABLED via MCP_ALLOW_NO_AUTH")
        else:
            raise SystemExit(
                "HTTP mode needs auth. Set MCP_AUTH_MODE=credentials (callers send their "
                "mailbox login), EMAIL_USERS_FILE for multi-user tokens, MCP_AUTH_TOKEN for "
                "single-tenant, or MCP_ALLOW_NO_AUTH=1 for dev."
            )
    log.info("path attachments: %s", f"only under {attach_dir}" if attach_dir else "disabled")

    resource_url = os.environ.get("MCP_RESOURCE_URL", "").strip().rstrip("/")
    as_urls = [u.strip() for u in os.environ.get("MCP_OAUTH_AUTHORIZATION_SERVERS", "").split(",") if u.strip()]
    proxy = oauth_proxy.from_env(resource_url) if resource_url else None
    if proxy is not None:
        log.info("OAuth DCR proxy ENABLED upstream=%s", proxy.upstream_issuer)
    if resource_url:
        _set_www_authenticate(resource_url)
        return WellKnownASGI(app, resource_url=resource_url,
                             authorization_servers=as_urls, proxy=proxy)
    return WellKnownASGI(app, resource_url="", authorization_servers=[])


def serve() -> None:
    """Dispatch to the chosen transport. Env vars documented in deploy/README.md."""
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rate_limit.init_from_env()
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("", "stdio"):
        # Local mode: the caller is the machine's own user.
        attach_dir = os.environ.get("MCP_ATTACHMENT_DIR", "").strip() or None
        smtp_ops.configure_path_attachments(True, attach_dir)
        mcp.run()
        return
    if transport == "http":
        transport = "streamable-http"
    if transport not in ("streamable-http", "sse"):
        raise SystemExit(f"unknown MCP_TRANSPORT={transport!r}. Use stdio|http|sse.")

    import uvicorn

    app = create_http_app(transport)
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8080"))
    log.info("cpanel-mail-mcp listening on http://%s:%s (transport=%s)", host, port, transport)
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True)


def main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "admin":
        from .admin import main as admin_main
        sys.exit(admin_main(sys.argv[2:]))
    serve()


if __name__ == "__main__":
    main()
