"""Pydantic input/output models for MCP tool schemas.

Every tool declares typed inputs and (where useful) typed outputs. FastMCP
turns these into JSON Schema for `tools/list` and validates client calls
against them, and modern MCP clients render `structuredContent` from the
declared output shape.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Attachments ────────────────────────────────────────────────────────

AttachmentIn = dict  # {path|content|content_base64, name?, mime?}


# ── Common output shapes ───────────────────────────────────────────────


class AccountInfo(BaseModel):
    name: str
    user: str
    smtp_host: str
    imap_host: str
    sent_folder: str
    drafts_folder: str


class ListAccountsResult(BaseModel):
    accounts: list[AccountInfo]


class FolderInfo(BaseModel):
    name: str = Field(description="Decoded folder name (UTF-7 → Unicode).")
    raw: str = Field(description="Wire-format name (may be UTF-7 encoded).")
    delimiter: str
    flags: str


class ListFoldersResult(BaseModel):
    account: str
    folders: list[FolderInfo]


class MessageSummary(BaseModel):
    uid: str
    from_: str = Field(alias="from")
    to: str
    cc: str = ""
    subject: str
    date: str | None = None

    model_config = {"populate_by_name": True}


class ListRecentResult(BaseModel):
    account: str
    folder: str
    messages: list[MessageSummary]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Pass as `cursor` to the next `list_recent` call to fetch the "
            "previous page (older messages). null when there are no more."
        ),
    )


class SearchResult(BaseModel):
    account: str
    folder: str
    field: str
    query: str
    results: list[MessageSummary]


class AttachmentMeta(BaseModel):
    filename: str
    mime: str
    size: int | None = None
    content_base64: str | None = Field(
        default=None,
        description="Base64-encoded content. Only populated when include_attachments=true.",
    )


class ReadEmailResult(BaseModel):
    uid: str
    from_: str = Field(alias="from")
    to: str
    cc: str = ""
    subject: str
    date: str | None = None
    body_text: str
    body_html: str
    attachments: list[AttachmentMeta]

    model_config = {"populate_by_name": True}


class DownloadAttachmentsResult(BaseModel):
    account: str
    uid: str
    folder: str
    attachments: list[AttachmentMeta]


class SendResult(BaseModel):
    ok: bool
    account: str
    recipients: list[str]
    message_id: str | None = None
    saved_to_sent: dict | None = None
    idempotent_replay: bool = Field(
        default=False,
        description="True when this response was replayed from an earlier identical call within the idempotency window.",
    )


class SaveDraftResult(BaseModel):
    ok: bool
    account: str
    folder: str
    response: str | None = None
    error: str | None = None


class FlagResult(BaseModel):
    ok: bool
    account: str
    uid: str
    folder: str
    flags_after: list[str] = []


class MoveResult(BaseModel):
    ok: bool
    account: str
    uid: str
    source_folder: str
    destination_folder: str
    new_uid: str | None = None


class DeleteResult(BaseModel):
    ok: bool
    account: str
    uid: str
    folder: str
    permanently_deleted: bool = Field(
        description="True if hard-deleted (Expunge). False if only moved to Trash."
    )


class FolderMutationResult(BaseModel):
    ok: bool
    account: str
    folder: str
    action: Literal["create", "delete", "rename"]
    new_name: str | None = None


class ThreadResult(BaseModel):
    account: str
    folder: str
    root_uid: str
    subject: str
    messages: list[MessageSummary]


# ── Error envelope ─────────────────────────────────────────────────────


class ToolError(BaseModel):
    """Structured error returned inside `isError=true` tool responses.

    Every field except `error` is optional. `hint` should tell the caller
    exactly what to do next (`call list_folders to see valid names`, etc).
    """

    error: str
    hint: str | None = None
    code: str | None = None
    context: dict[str, Any] | None = None
