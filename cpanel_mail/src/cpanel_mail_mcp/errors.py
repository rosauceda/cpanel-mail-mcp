"""Actionable-error helpers.

Every user-facing error we raise carries a `hint` string suggesting the
next tool call the agent can make to unstick itself. FastMCP turns a raised
exception into the tool's error text via `str(e)`, so `__str__` carries the
hint and code along with the message.
"""
from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """Base class for user-visible tool errors.

    Args:
        error: One-line description of what went wrong.
        hint:  Recommended next action (e.g. "call list_folders to see
               valid names"). Displayed to the calling LLM verbatim.
        code:  Short machine-readable code (e.g. "folder_not_found").
        context: Anything useful for debugging.
    """

    def __init__(
        self,
        error: str,
        *,
        hint: str | None = None,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.hint = hint
        self.code = code
        self.context = context

    def __str__(self) -> str:
        s = self.error
        if self.hint:
            s += f"\nHint: {self.hint}"
        if self.code:
            s += f"\nCode: {self.code}"
        return s

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": self.error}
        if self.hint:
            d["hint"] = self.hint
        if self.code:
            d["code"] = self.code
        if self.context:
            d["context"] = self.context
        return d


class FolderNotFound(ToolError):
    def __init__(self, folder: str, account: str) -> None:
        super().__init__(
            f"folder {folder!r} not found in account {account!r}",
            hint="Call `list_folders` to see the exact folder names for this account. "
            "Names are case-sensitive and may include UTF-7 encoding.",
            code="folder_not_found",
            context={"folder": folder, "account": account},
        )


class MessageNotFound(ToolError):
    def __init__(self, uid: str, folder: str) -> None:
        super().__init__(
            f"message uid={uid!r} not found in folder {folder!r}",
            hint=f"Call `list_recent` on {folder!r} to see current UIDs; they change when "
            "messages are moved/expunged.",
            code="message_not_found",
            context={"uid": uid, "folder": folder},
        )


class InvalidField(ToolError):
    def __init__(self, field: str, allowed: list[str]) -> None:
        super().__init__(
            f"invalid field {field!r}",
            hint=f"Use one of: {', '.join(allowed)}",
            code="invalid_field",
            context={"field": field, "allowed": allowed},
        )


class AttachmentTooLarge(ToolError):
    def __init__(self, actual: int, limit: int) -> None:
        super().__init__(
            f"attachments total {actual} bytes exceeds server limit of {limit}",
            hint=f"Reduce total attachment size below {limit // (1024 * 1024)} MB, "
            "or split into multiple messages.",
            code="attachment_too_large",
            context={"actual_bytes": actual, "limit_bytes": limit},
        )


class SendGateBlocked(ToolError):
    def __init__(self) -> None:
        super().__init__(
            "send blocked by EMAIL_SEND_CONFIRMATION_CODE gate",
            hint="Pass `confirm=<code>` matching the value the operator set in "
            "EMAIL_SEND_CONFIRMATION_CODE.",
            code="send_gate_blocked",
        )


class RateLimited(ToolError):
    def __init__(self, retry_after_s: int, bucket: str) -> None:
        super().__init__(
            f"rate limit exceeded for {bucket!r} bucket",
            hint=f"Wait ~{retry_after_s}s and retry. Rate limits are per token and per "
            "bucket (send vs. read).",
            code="rate_limited",
            context={"retry_after_seconds": retry_after_s, "bucket": bucket},
        )


class InvalidUid(ToolError):
    def __init__(self, value: str, what: str = "uid") -> None:
        super().__init__(
            f"invalid {what} {value!r}: must be a single numeric IMAP UID",
            hint="Use the `uid` values returned by `list_recent` / `search_emails`. "
            "Ranges and wildcards (e.g. `1:*`) are not accepted.",
            code="invalid_uid",
            context={what: value},
        )


class InvalidSince(ToolError):
    def __init__(self, value: str) -> None:
        super().__init__(
            f"invalid since {value!r}",
            hint="Use an ISO date/datetime (2026-09-24, 2026-09-24T08:00:00-07:00) "
            "or a relative span: 30m, 2h, 1d, 1w.",
            code="invalid_since",
            context={"since": value},
        )


class TrashNotFound(ToolError):
    def __init__(self, account: str) -> None:
        super().__init__(
            f"no Trash folder found for account {account!r}; nothing was deleted",
            hint="Call `list_folders` and pass the right folder as `trash_folder`, "
            "or pass `permanent=true` to delete for good.",
            code="trash_not_found",
            context={"account": account},
        )


class AttachmentPathNotAllowed(ToolError):
    def __init__(self, path: str, root: str | None) -> None:
        if root:
            msg = f"attachment path {path!r} is outside the allowed directory {root!r}"
            hint = f"Place the file under {root} or send it as `content_base64`."
        else:
            msg = f"attachments by `path` are disabled on this server ({path!r})"
            hint = "Send the file as `content_base64` (or `content` for text) instead."
        super().__init__(msg, hint=hint, code="attachment_path_not_allowed",
                         context={"path": path, "allowed_root": root})


class UnknownAccount(ToolError):
    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(
            f"account {name!r} is not configured",
            hint=f"Available accounts: {', '.join(available) if available else '(none)'}. "
            "Call `list_accounts` for details.",
            code="unknown_account",
            context={"name": name, "available": available},
        )
