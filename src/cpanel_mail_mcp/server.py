"""cpanel-mail-mcp — MCP server exposing per-operation tools for IMAP/SMTP.

Each mailbox action is its own MCP tool (not a single dispatcher). This lets
`tools/list` show the full capability surface and lets clients render the
right shape of input/output per tool.

Transports: stdio (default) or Streamable HTTP with bearer + optional
Cloudflare Access OIDC (see `serve()`).
"""
from __future__ import annotations

import contextvars
import hmac
import logging
import os
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import cf_access, idempotency, imap_ops, oauth_proxy, rate_limit, smtp_ops
from . import users as users_mod
from .accounts import Account, get_account, load_accounts
from .errors import (
    FolderNotFound,
    InvalidField,
    MessageNotFound,
    RateLimited,
    SendGateBlocked,
    ToolError,
    UnknownAccount,
)
from .models import (
    AccountInfo,
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

_current_account_var: contextvars.ContextVar[Account | None] = contextvars.ContextVar(
    "cpanel_mail_current_account", default=None
)
_current_identity_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cpanel_mail_current_identity", default="anonymous"
)


def _transport_security() -> TransportSecuritySettings | None:
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    origins = [o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    disabled = os.environ.get("MCP_DISABLE_DNS_REBINDING_PROTECTION", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not hosts and not origins and not disabled:
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


def _pick_account(account_param: str | None) -> Account:
    forced = _current_account_var.get()
    if forced is not None:
        return forced
    try:
        return get_account(_accounts(), account_param)
    except ValueError as e:
        available = list(_accounts().keys())
        raise UnknownAccount(account_param or "", available) from e


def _check_send_gate(confirm: str | None) -> None:
    code = os.environ.get("EMAIL_SEND_CONFIRMATION_CODE")
    if code and confirm != code:
        raise SendGateBlocked()


def _rl(bucket: str) -> None:
    rate_limit.limiter.check(bucket, _current_identity_var.get())


# ── Read-only tools ────────────────────────────────────────────────────


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True,
    }
)
def list_accounts() -> ListAccountsResult:
    """List email accounts this caller can act on (no secrets returned).

    In multi-user OAuth mode, only the caller's own account is returned;
    in single-tenant mode, all configured accounts appear.
    """
    forced = _current_account_var.get()
    accts = [forced] if forced is not None else list(_accounts().values())
    return ListAccountsResult(
        accounts=[
            AccountInfo(
                name=a.name, user=a.user, smtp_host=a.smtp_host,
                imap_host=a.imap_host, sent_folder=a.sent_folder,
                drafts_folder=a.drafts_folder,
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
def list_folders(
    account: Annotated[str | None, Field(description="Account name (list_accounts). Omit to use the default.")] = None,
) -> ListFoldersResult:
    """List every IMAP folder (mailbox) the account can see, UTF-7 decoded."""
    _rl("read")
    a = _pick_account(account)
    folders = imap_ops.list_folders(a)
    return ListFoldersResult(
        account=a.name,
        folders=[FolderInfo(**f) for f in folders],
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
def list_recent(
    folder: Annotated[str, Field(description="IMAP folder name. Case-sensitive.")] = "INBOX",
    limit: Annotated[int, Field(ge=1, le=200, description="Max messages per page.")] = 20,
    cursor: Annotated[str | None, Field(description="Pass the previous response's `next_cursor` to page older.")] = None,
    account: Annotated[str | None, Field(description="Account name; omit for default.")] = None,
) -> ListRecentResult:
    """List the most recent messages in a folder (metadata only, newest first).

    Use `cursor` (=previous `next_cursor`) to page further back in time.
    """
    _rl("read")
    a = _pick_account(account)
    res = imap_ops.list_recent(a, folder, limit, cursor=cursor)
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
def search_emails(
    query: Annotated[str, Field(min_length=1, description="Search term.")],
    field: Annotated[str, Field(description="One of FROM, TO, SUBJECT, BODY, TEXT.")] = "SUBJECT",
    folder: Annotated[str, Field(description="IMAP folder to search.")] = "INBOX",
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
    account: Annotated[str | None, Field()] = None,
) -> SearchResult:
    """IMAP SEARCH over one field. Wraps `query` in quotes for IMAP.

    Note: IMAP SEARCH is substring, case-insensitive on most servers, and
    doesn't support boolean operators. For richer queries, chain calls.
    """
    _rl("read")
    a = _pick_account(account)
    results = imap_ops.search(a, query, field, folder, limit)
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
def read_email(
    uid: Annotated[str, Field(description="Message UID (from list_recent/search).")],
    folder: Annotated[str, Field()] = "INBOX",
    include_attachments: Annotated[bool, Field(description="If true, embed attachments as base64.")] = False,
    account: Annotated[str | None, Field()] = None,
) -> ReadEmailResult:
    """Fetch full headers + body of a message. Attachments listed by name+mime.

    Set `include_attachments=true` to also embed each attachment as base64
    (respects the size limit; large attachments may push you over context).
    """
    _rl("read")
    a = _pick_account(account)
    data = imap_ops.read_email(a, uid, folder, include_attachments)
    return ReadEmailResult(**data)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    }
)
def download_attachments(
    uid: Annotated[str, Field(description="Message UID.")],
    folder: Annotated[str, Field()] = "INBOX",
    filenames: Annotated[list[str] | None, Field(description="Only fetch these filenames.")] = None,
    account: Annotated[str | None, Field()] = None,
) -> DownloadAttachmentsResult:
    """Return message attachments as base64. Filter by filenames to save bytes."""
    _rl("read")
    a = _pick_account(account)
    atts = imap_ops.download_attachments(a, uid, folder, filenames)
    from .models import AttachmentMeta
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
def get_thread(
    uid: Annotated[str, Field(description="Any message UID in the thread.")],
    folder: Annotated[str, Field()] = "INBOX",
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    account: Annotated[str | None, Field()] = None,
) -> ThreadResult:
    """Group messages sharing Message-ID / References / In-Reply-To headers."""
    _rl("read")
    a = _pick_account(account)
    data = imap_ops.get_thread(a, uid, folder, limit)
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
    except Exception as e:  # non-fatal
        return {"ok": False, "error": str(e)}


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,  # via idempotency_key
        "openWorldHint": True,
    }
)
def send_email(
    to: Annotated[str, Field(min_length=1, description="Recipient(s), comma-separated.")],
    subject: Annotated[str, Field()] = "",
    text: Annotated[str | None, Field(description="Plain-text body.")] = None,
    html: Annotated[str | None, Field(description="HTML body; sent as multipart/alternative when both are set.")] = None,
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    reply_to: Annotated[str | None, Field()] = None,
    attachments: Annotated[list[dict] | None, Field(description="[{path|content|content_base64, name?, mime?}, ...]")] = None,
    save_to_sent: Annotated[bool | None, Field(description="Override the account's Save-to-Sent default.")] = None,
    confirm: Annotated[str | None, Field(description="Send-gate code if EMAIL_SEND_CONFIRMATION_CODE is set.")] = None,
    idempotency_key: Annotated[str | None, Field(description="Opaque key; identical (key, caller) within 5 min returns the cached result.")] = None,
    account: Annotated[str | None, Field()] = None,
) -> SendResult:
    """Send an email. Supports HTML, attachments, and Save-to-Sent.

    On retries after a client timeout, pass the same `idempotency_key` you
    used the first time to prevent duplicate delivery.
    """
    _check_send_gate(confirm)
    _rl("send")
    a = _pick_account(account)
    identity = _current_identity_var.get()
    cached = idempotency.store.get(identity, idempotency_key or "")
    if cached:
        return SendResult(**cached, idempotent_replay=True)

    result = smtp_ops.send(
        a, to, subject, text, html, cc, bcc, reply_to, attachments,
    )
    raw = result.pop("raw")
    saved = _save_to_sent_if_enabled(a, raw, save_to_sent)
    payload = {
        "ok": True,
        "account": a.name,
        "recipients": result["recipients"],
        "message_id": result.get("message_id"),
        "saved_to_sent": saved,
    }
    idempotency.store.put(identity, idempotency_key or "", payload)
    return SendResult(**payload)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def reply_email(
    uid: Annotated[str, Field(description="UID of the message to reply to.")],
    text: Annotated[str | None, Field()] = None,
    html: Annotated[str | None, Field()] = None,
    folder: Annotated[str, Field()] = "INBOX",
    reply_all: Annotated[bool, Field(description="Include original To/Cc in the reply.")] = False,
    attachments: Annotated[list[dict] | None, Field()] = None,
    save_to_sent: Annotated[bool | None, Field()] = None,
    confirm: Annotated[str | None, Field()] = None,
    idempotency_key: Annotated[str | None, Field()] = None,
    account: Annotated[str | None, Field()] = None,
) -> SendResult:
    """Reply to a message. Preserves Message-ID linking + Subject `Re:` prefix.

    `reply_all=true` includes the original To and Cc in the reply.
    """
    _check_send_gate(confirm)
    _rl("send")
    a = _pick_account(account)
    identity = _current_identity_var.get()
    cached = idempotency.store.get(identity, idempotency_key or "")
    if cached:
        return SendResult(**cached, idempotent_replay=True)

    orig = imap_ops.read_email(a, uid, folder, include_attachments=False)
    subject = orig["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    to = orig["from"]
    cc = None
    if reply_all:
        extra = [x for x in [orig.get("to"), orig.get("cc")] if x]
        cc = ", ".join(extra) if extra else None
    in_reply_to = _extract_msgid(orig)
    references = in_reply_to or ""

    result = smtp_ops.send(
        a, to, subject, text, html, cc, None, None, attachments,
        in_reply_to=in_reply_to, references=references,
    )
    raw = result.pop("raw")
    saved = _save_to_sent_if_enabled(a, raw, save_to_sent)
    payload = {
        "ok": True, "account": a.name,
        "recipients": result["recipients"],
        "message_id": result.get("message_id"),
        "saved_to_sent": saved,
    }
    idempotency.store.put(identity, idempotency_key or "", payload)
    return SendResult(**payload)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def forward_email(
    uid: Annotated[str, Field(description="UID of the message to forward.")],
    to: Annotated[str, Field(min_length=1)],
    text: Annotated[str | None, Field(description="Additional note to include above the forwarded body.")] = None,
    folder: Annotated[str, Field()] = "INBOX",
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    save_to_sent: Annotated[bool | None, Field()] = None,
    confirm: Annotated[str | None, Field()] = None,
    idempotency_key: Annotated[str | None, Field()] = None,
    account: Annotated[str | None, Field()] = None,
) -> SendResult:
    """Forward a message. Subject gets `Fwd:` prefix; original headers quoted."""
    _check_send_gate(confirm)
    _rl("send")
    a = _pick_account(account)
    identity = _current_identity_var.get()
    cached = idempotency.store.get(identity, idempotency_key or "")
    if cached:
        return SendResult(**cached, idempotent_replay=True)

    orig = imap_ops.read_email(a, uid, folder, include_attachments=True)
    subject = orig["subject"] or ""
    if not subject.lower().startswith(("fwd:", "fw:")):
        subject = f"Fwd: {subject}"
    prefix = (text or "").rstrip() + "\n\n" if text else ""
    quoted = (
        f"---------- Forwarded message ----------\n"
        f"From: {orig.get('from','')}\n"
        f"Date: {orig.get('date','')}\n"
        f"Subject: {orig.get('subject','')}\n"
        f"To: {orig.get('to','')}\n\n"
        f"{orig.get('body_text','') or orig.get('body_html','')}"
    )
    forward_body = prefix + quoted
    # attach the original attachments too
    fwd_atts: list[dict] = []
    for att in orig.get("attachments", []):
        if att.get("content_base64"):
            fwd_atts.append({
                "name": att["filename"],
                "content_base64": att["content_base64"],
                "mime": att.get("mime"),
            })
    result = smtp_ops.send(a, to, subject, forward_body, None, cc, bcc, None, fwd_atts)
    raw = result.pop("raw")
    saved = _save_to_sent_if_enabled(a, raw, save_to_sent)
    payload = {
        "ok": True, "account": a.name,
        "recipients": result["recipients"],
        "message_id": result.get("message_id"),
        "saved_to_sent": saved,
    }
    idempotency.store.put(identity, idempotency_key or "", payload)
    return SendResult(**payload)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def save_draft(
    to: Annotated[str, Field()] = "",
    subject: Annotated[str, Field()] = "",
    text: Annotated[str | None, Field()] = None,
    html: Annotated[str | None, Field()] = None,
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    attachments: Annotated[list[dict] | None, Field()] = None,
    folder: Annotated[str | None, Field(description="Override Drafts folder (default: account.drafts_folder).")] = None,
    account: Annotated[str | None, Field()] = None,
) -> SaveDraftResult:
    """Save an unsent draft in the account's Drafts folder."""
    _rl("send")
    a = _pick_account(account)
    msg = smtp_ops.build_message(a, to, subject, text, html, cc, bcc, None, attachments)
    box = folder or a.drafts_folder
    result = imap_ops.append_message(a, box, msg.as_bytes(), "\\Draft \\Seen")
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
def send_invite(
    to: Annotated[str, Field(min_length=1)],
    subject: Annotated[str, Field(min_length=1)],
    start: Annotated[str, Field(description="ISO 8601 or 'YYYY-MM-DD HH:MM'. Naive = UTC.")],
    end: Annotated[str, Field()],
    description: Annotated[str, Field()] = "",
    location: Annotated[str, Field()] = "",
    organizer: Annotated[str | None, Field()] = None,
    attendees: Annotated[list[str] | None, Field()] = None,
    cc: Annotated[str | None, Field()] = None,
    bcc: Annotated[str | None, Field()] = None,
    text: Annotated[str | None, Field()] = None,
    html: Annotated[str | None, Field()] = None,
    save_to_sent: Annotated[bool | None, Field()] = None,
    confirm: Annotated[str | None, Field()] = None,
    idempotency_key: Annotated[str | None, Field()] = None,
    account: Annotated[str | None, Field()] = None,
) -> SendResult:
    """Send a calendar invite (RFC 5545 ICS, METHOD:REQUEST)."""
    _check_send_gate(confirm)
    _rl("send")
    a = _pick_account(account)
    identity = _current_identity_var.get()
    cached = idempotency.store.get(identity, idempotency_key or "")
    if cached:
        return SendResult(**cached, idempotent_replay=True)

    result = smtp_ops.send_invite(
        a, to, subject, start, end, description, location, organizer, attendees,
        cc, bcc, text, html,
    )
    raw = result.pop("raw")
    saved = _save_to_sent_if_enabled(a, raw, save_to_sent)
    payload = {
        "ok": True, "account": a.name,
        "recipients": result["recipients"],
        "message_id": result.get("message_id"),
        "saved_to_sent": saved,
    }
    idempotency.store.put(identity, idempotency_key or "", payload)
    return SendResult(**payload)


# ── Flag / move / delete ───────────────────────────────────────────────


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def mark_read(
    uid: Annotated[str, Field()],
    folder: Annotated[str, Field()] = "INBOX",
    account: Annotated[str | None, Field()] = None,
) -> FlagResult:
    """Add the \\Seen flag."""
    _rl("read")
    a = _pick_account(account)
    return FlagResult(**imap_ops.mark_read(a, uid, folder))


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def mark_unread(
    uid: Annotated[str, Field()],
    folder: Annotated[str, Field()] = "INBOX",
    account: Annotated[str | None, Field()] = None,
) -> FlagResult:
    """Remove the \\Seen flag."""
    _rl("read")
    a = _pick_account(account)
    return FlagResult(**imap_ops.mark_unread(a, uid, folder))


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def star_email(
    uid: Annotated[str, Field()],
    folder: Annotated[str, Field()] = "INBOX",
    account: Annotated[str | None, Field()] = None,
) -> FlagResult:
    """Add the \\Flagged (starred) flag."""
    _rl("read")
    a = _pick_account(account)
    return FlagResult(**imap_ops.star(a, uid, folder))


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def unstar_email(
    uid: Annotated[str, Field()],
    folder: Annotated[str, Field()] = "INBOX",
    account: Annotated[str | None, Field()] = None,
) -> FlagResult:
    """Remove the \\Flagged flag."""
    _rl("read")
    a = _pick_account(account)
    return FlagResult(**imap_ops.unstar(a, uid, folder))


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
)
def move_email(
    uid: Annotated[str, Field()],
    source_folder: Annotated[str, Field()],
    destination_folder: Annotated[str, Field()],
    account: Annotated[str | None, Field()] = None,
) -> MoveResult:
    """Move a message between folders (RFC 6851 MOVE with COPY+EXPUNGE fallback)."""
    _rl("read")
    a = _pick_account(account)
    return MoveResult(**imap_ops.move_message(a, uid, source_folder, destination_folder))


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
def copy_email(
    uid: Annotated[str, Field()],
    source_folder: Annotated[str, Field()],
    destination_folder: Annotated[str, Field()],
    account: Annotated[str | None, Field()] = None,
) -> MoveResult:
    """Copy a message to another folder without removing the original."""
    _rl("read")
    a = _pick_account(account)
    r = imap_ops.copy_message(a, uid, source_folder, destination_folder)
    return MoveResult(**r)


@mcp.tool(
    annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
)
def delete_email(
    uid: Annotated[str, Field()],
    folder: Annotated[str, Field()] = "INBOX",
    permanent: Annotated[bool, Field(description="If false (default), move to Trash. If true, expunge in place.")] = False,
    trash_folder: Annotated[str, Field()] = "INBOX.Trash",
    account: Annotated[str | None, Field()] = None,
) -> DeleteResult:
    """Delete a message. Soft-delete by default (moves to Trash)."""
    _rl("read")
    a = _pick_account(account)
    r = imap_ops.delete_message(a, uid, folder, permanent=permanent, trash_folder=trash_folder)
    return DeleteResult(**{k: r[k] for k in ("ok", "account", "uid", "folder", "permanently_deleted")})


# ── Folder management ──────────────────────────────────────────────────


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
def create_folder(
    folder: Annotated[str, Field(min_length=1)],
    account: Annotated[str | None, Field()] = None,
) -> FolderMutationResult:
    """Create a new IMAP folder and subscribe to it."""
    _rl("read")
    a = _pick_account(account)
    return FolderMutationResult(**imap_ops.create_folder(a, folder))


@mcp.tool(
    annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
)
def delete_folder(
    folder: Annotated[str, Field(min_length=1)],
    account: Annotated[str | None, Field()] = None,
) -> FolderMutationResult:
    """Delete an empty IMAP folder. Fails if not empty (per RFC)."""
    _rl("read")
    a = _pick_account(account)
    return FolderMutationResult(**imap_ops.delete_folder(a, folder))


@mcp.tool(
    annotations={"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
def rename_folder(
    folder: Annotated[str, Field(min_length=1)],
    new_name: Annotated[str, Field(min_length=1)],
    account: Annotated[str | None, Field()] = None,
) -> FolderMutationResult:
    """Rename an IMAP folder."""
    _rl("read")
    a = _pick_account(account)
    return FolderMutationResult(**imap_ops.rename_folder(a, folder, new_name))


# ── ASGI middleware for HTTP transport ─────────────────────────────────


def _extract_msgid(msg: dict) -> str | None:
    for k in ("message_id", "Message-ID", "message-id"):
        v = msg.get(k)
        if v:
            return v
    return None


async def _send_json(send, status: int, payload: bytes, extra_headers: list | None = None) -> None:
    headers = [(b"content-type", b"application/json")]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


_www_authenticate_header: bytes = b'Bearer realm="mcp"'


def _set_www_authenticate(resource_url: str) -> None:
    global _www_authenticate_header
    if resource_url:
        _www_authenticate_header = (
            f'Bearer realm="mcp", '
            f'resource_metadata="{resource_url}/.well-known/oauth-protected-resource"'
        ).encode()


async def _send_401(send, detail: str = "unauthorized") -> None:
    body = f'{{"error":"{detail}"}}'.encode()
    await _send_json(send, 401, body, [(b"www-authenticate", _www_authenticate_header)])


async def _send_403(send, detail: str) -> None:
    body = f'{{"error":"{detail}"}}'.encode()
    await _send_json(send, 403, body)


def _user_by_email(users: dict[str, Account], email: str) -> Account | None:
    needle = email.lower()
    for acct in users.values():
        if needle in acct.sso_emails or acct.user.lower() == needle:
            return acct
    return None


class UnifiedAuthASGI:
    """CF Access JWT (with fallback to opaque bearer) → binds current Account
    and identity string in contextvars for handlers + rate limiter."""

    def __init__(
        self,
        app,
        *,
        users: dict[str, Account] | None = None,
        single_token: str | None = None,
        cf_verifier: cf_access.CFAccessVerifier | None = None,
    ) -> None:
        if bool(single_token) == bool(users):
            raise ValueError("UnifiedAuthASGI: pick exactly one of single_token or users")
        self.app = app
        self._users = users or {}
        self._expected = f"Bearer {single_token}".encode() if single_token else None
        self._cf = cf_verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in ("/health", "/healthz") or path.startswith("/.well-known/") or path == "/register":
            await self.app(scope, receive, send)
            return

        cf_jwt_header = b""
        auth = b""
        for k, v in scope.get("headers", []):
            if k == b"cf-access-jwt-assertion":
                cf_jwt_header = v
            elif k == b"authorization":
                auth = v

        if self._cf and cf_jwt_header:
            try:
                claims = self._cf.verify(cf_jwt_header.decode(errors="replace").strip())
            except cf_access.CFAccessInvalid as e:
                await _send_401(send, f"cf-access-jwt-invalid: {e}")
                return
            return await self._dispatch_with_claims(scope, receive, send, claims)

        if self._cf and auth.startswith(b"Bearer "):
            token = auth[7:].decode("ascii", errors="replace").strip()
            if token.count(".") == 2:
                try:
                    claims = self._cf.verify(token)
                    return await self._dispatch_with_claims(scope, receive, send, claims)
                except cf_access.CFAccessInvalid:
                    pass

        if self._users and auth.startswith(b"Bearer "):
            token = auth[7:].decode("ascii", errors="replace").strip()
            acct = self._users.get(token)
            if acct is not None:
                return await self._dispatch_with_account(scope, receive, send, acct, identity=f"bearer:{token[:8]}")

        if self._expected is not None and hmac.compare_digest(auth, self._expected):
            await self.app(scope, receive, send)
            return

        await _send_401(send)

    async def _dispatch_with_claims(self, scope, receive, send, claims: dict) -> None:
        email = cf_access.extract_email(claims)
        if not email:
            await _send_403(send, "cf-access-jwt-no-email")
            return
        acct = _user_by_email(self._users, email) if self._users else None
        if acct is None:
            await _send_403(send, f"unknown-user:{email}")
            return
        await self._dispatch_with_account(scope, receive, send, acct, identity=f"email:{email}")

    async def _dispatch_with_account(self, scope, receive, send, acct: Account, *, identity: str) -> None:
        reset_a = _current_account_var.set(acct)
        reset_i = _current_identity_var.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_account_var.reset(reset_a)
            _current_identity_var.reset(reset_i)


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
        body = {
            "resource": resource_url,
            "authorization_servers": as_list,
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/rosauceda/cpanel-mail-mcp",
        }
        import json
        self._prm_body = json.dumps(body).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path in ("/health", "/healthz"):
            await _send_json(send, 200, b"ok", [(b"content-type", b"text/plain; charset=utf-8")])
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
                    body = self.proxy.composed_metadata()
                except Exception as e:
                    await _send_json(send, 502, f'{{"error":"upstream_unreachable","detail":"{e}"}}'.encode())
                    return
                await _send_json(send, 200, body)
                return
            if path == "/register" and method == "POST":
                try:
                    await self.proxy.handle_register(scope, receive, send)
                except Exception as e:
                    log.exception("DCR handler crashed: %s", e)
                    await _send_json(send, 500, b'{"error":"dcr_failed"}')
                return
        await self.app(scope, receive, send)


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def serve() -> None:
    """Dispatch to the chosen transport. Env vars documented in deploy/README.md."""
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rate_limit.init_from_env()
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("", "stdio"):
        mcp.run()
        return
    if transport == "http":
        transport = "streamable-http"
    if transport not in ("streamable-http", "sse"):
        raise SystemExit(f"unknown MCP_TRANSPORT={transport!r}. Use stdio|http|sse.")

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8080"))
    allow_no_auth = _bool_env("MCP_ALLOW_NO_AUTH")

    multi_user = users_mod.is_multi_user()
    users_map: dict[str, Account] = {}
    single_token = ""
    if multi_user:
        users_map = users_mod.load_users()
        if not users_map and not allow_no_auth:
            raise SystemExit(
                f"EMAIL_USERS_FILE={users_mod.users_path()} exists but is empty. "
                "Add a user with:  cpanel-mail-mcp admin add-user --email … --host …"
            )
    else:
        single_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
        if not single_token and not allow_no_auth:
            raise SystemExit(
                "HTTP mode needs auth. Set EMAIL_USERS_FILE for multi-user, "
                "MCP_AUTH_TOKEN for single-tenant, or MCP_ALLOW_NO_AUTH=1 for dev."
            )

    import uvicorn

    cf_verifier = cf_access.from_env()
    if cf_verifier:
        log.info("CF Access OIDC ENABLED: team=%s aud=%s…",
                 cf_verifier.team_domain, cf_verifier.audience[:10])

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    if multi_user and users_map:
        app = UnifiedAuthASGI(app, users=users_map, cf_verifier=cf_verifier)
        log.info("multi-user mode: %d user(s) loaded", len(users_map))
    elif single_token:
        app = UnifiedAuthASGI(app, single_token=single_token, cf_verifier=cf_verifier)
        log.info("single-tenant mode: bearer auth ENABLED")
    else:
        log.warning("bearer auth DISABLED via MCP_ALLOW_NO_AUTH")

    resource_url = os.environ.get("MCP_RESOURCE_URL", "").strip().rstrip("/")
    as_urls = [u.strip() for u in os.environ.get("MCP_OAUTH_AUTHORIZATION_SERVERS", "").split(",") if u.strip()]
    proxy = oauth_proxy.from_env(resource_url) if resource_url else None
    if proxy is not None:
        log.info("OAuth DCR proxy ENABLED upstream=%s", proxy.upstream_issuer)
    if resource_url:
        _set_www_authenticate(resource_url)
        app = WellKnownASGI(app, resource_url=resource_url,
                            authorization_servers=as_urls, proxy=proxy)
    else:
        app = WellKnownASGI(app, resource_url="", authorization_servers=[])

    log.info("cpanel-mail-mcp listening on http://%s:%s (transport=%s)", host, port, transport)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "admin":
        from .admin import main as admin_main
        sys.exit(admin_main(sys.argv[2:]))
    serve()


if __name__ == "__main__":
    main()
