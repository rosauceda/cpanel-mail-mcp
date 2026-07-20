"""cpanel-mail-mcp — single MCP tool `email` with lazy action discovery.

Design: one tool, one `action` string, one `params` dict. The tool schema
stays tiny so it costs the client almost nothing in context; the client
calls `action='help'` to discover what each action wants.
"""
from __future__ import annotations

import hmac
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from . import imap_ops, smtp_ops
from .accounts import Account, get_account, load_accounts

log = logging.getLogger("cpanel_mail_mcp")


def _load_env() -> None:
    """Load `.env` from `EMAIL_ENV_FILE` if set, else from cwd."""
    p = os.environ.get("EMAIL_ENV_FILE")
    if p and Path(p).is_file():
        load_dotenv(p, override=False)
    else:
        load_dotenv(override=False)


_load_env()

mcp = FastMCP("cpanel-mail")

_accounts_cache: dict[str, Account] | None = None


def _accounts() -> dict[str, Account]:
    global _accounts_cache
    if _accounts_cache is None:
        _accounts_cache = load_accounts()
    return _accounts_cache


def _check_send_gate(confirm: str | None) -> None:
    code = os.environ.get("EMAIL_SEND_CONFIRMATION_CODE")
    if code and confirm != code:
        raise PermissionError(
            "Send blocked. Pass params.confirm='<code>' matching EMAIL_SEND_CONFIRMATION_CODE."
        )


ACTIONS: dict[str, tuple[Callable[..., Any], str]] = {}


def _action(name: str, description: str):
    def deco(fn):
        ACTIONS[name] = (fn, description)
        return fn

    return deco


@_action("help", "List available actions with their parameter signatures.")
def _act_help(**_: Any) -> dict:
    return {
        "actions": {
            name: {
                "description": desc,
                "signature": str(inspect.signature(fn)),
            }
            for name, (fn, desc) in ACTIONS.items()
        }
    }


@_action("list_accounts", "List configured accounts (names + hosts, no secrets).")
def _act_list_accounts(**_: Any) -> dict:
    return {
        "accounts": [
            {
                "name": a.name,
                "user": a.user,
                "smtp_host": a.smtp_host,
                "imap_host": a.imap_host,
                "sent_folder": a.sent_folder,
                "drafts_folder": a.drafts_folder,
            }
            for a in _accounts().values()
        ]
    }


@_action("list_folders", "List IMAP folders. Params: account?")
def _act_list_folders(account: str | None = None, **_: Any) -> dict:
    a = get_account(_accounts(), account)
    return {"account": a.name, "folders": imap_ops.list_folders(a)}


@_action(
    "list_recent",
    "List recent messages. Params: folder='INBOX', limit=20, account?",
)
def _act_list_recent(
    folder: str = "INBOX", limit: int = 20, account: str | None = None, **_: Any
) -> dict:
    a = get_account(_accounts(), account)
    return {
        "account": a.name,
        "folder": folder,
        "messages": imap_ops.list_recent(a, folder, limit),
    }


@_action(
    "search",
    "Search messages. Params: query, field=SUBJECT|FROM|TO|BODY|TEXT, folder='INBOX', limit=20, account?",
)
def _act_search(
    query: str,
    field: str = "SUBJECT",
    folder: str = "INBOX",
    limit: int = 20,
    account: str | None = None,
    **_: Any,
) -> dict:
    a = get_account(_accounts(), account)
    return {
        "account": a.name,
        "results": imap_ops.search(a, query, field, folder, limit),
    }


@_action(
    "read",
    "Read a message by UID. Params: uid, folder='INBOX', include_attachments=false, account?",
)
def _act_read(
    uid: str,
    folder: str = "INBOX",
    include_attachments: bool = False,
    account: str | None = None,
    **_: Any,
) -> dict:
    a = get_account(_accounts(), account)
    return imap_ops.read_email(a, uid, folder, include_attachments)


@_action(
    "download_attachments",
    "Fetch attachments as base64. Params: uid, folder='INBOX', filenames? (list to filter), account?",
)
def _act_download(
    uid: str,
    folder: str = "INBOX",
    filenames: list[str] | None = None,
    account: str | None = None,
    **_: Any,
) -> dict:
    a = get_account(_accounts(), account)
    return {
        "account": a.name,
        "attachments": imap_ops.download_attachments(a, uid, folder, filenames),
    }


@_action(
    "send",
    (
        "Send an email. Params: to, subject, text?, html?, cc?, bcc?, reply_to?, "
        "attachments? (list of {path|content|content_base64, name?, mime?}), "
        "save_to_sent?, confirm?, account?"
    ),
)
def _act_send(
    to: str,
    subject: str = "",
    text: str | None = None,
    html: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to: str | None = None,
    attachments: list[dict] | None = None,
    save_to_sent: bool | None = None,
    confirm: str | None = None,
    account: str | None = None,
    **_: Any,
) -> dict:
    _check_send_gate(confirm)
    a = get_account(_accounts(), account)
    result = smtp_ops.send(a, to, subject, text, html, cc, bcc, reply_to, attachments)
    raw = result.pop("raw")
    save = a.save_to_sent if save_to_sent is None else save_to_sent
    saved: dict | None = None
    if save:
        try:
            saved = imap_ops.append_message(a, a.sent_folder, raw, "\\Seen")
        except Exception as e:
            saved = {"ok": False, "error": str(e)}
    return {"account": a.name, "sent": result, "saved_to_sent": saved}


@_action(
    "save_draft",
    (
        "Save a draft (any field optional). Params: to?, subject?, text?, html?, cc?, bcc?, "
        "attachments?, folder? (default account.drafts_folder), account?"
    ),
)
def _act_save_draft(
    to: str = "",
    subject: str = "",
    text: str | None = None,
    html: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    attachments: list[dict] | None = None,
    folder: str | None = None,
    account: str | None = None,
    **_: Any,
) -> dict:
    a = get_account(_accounts(), account)
    msg = smtp_ops.build_message(a, to, subject, text, html, cc, bcc, None, attachments)
    box = folder or a.drafts_folder
    result = imap_ops.append_message(a, box, msg.as_bytes(), "\\Draft \\Seen")
    return {"account": a.name, **result}


@_action(
    "send_invite",
    (
        "Send a calendar invite (ICS, method=REQUEST). Params: to, subject, start, end, "
        "description?, location?, organizer?, attendees? (list), cc?, bcc?, text?, html?, "
        "save_to_sent?, confirm?, account?. Datetime accepts ISO 8601 or 'YYYY-MM-DD HH:MM'."
    ),
)
def _act_send_invite(
    to: str,
    subject: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer: str | None = None,
    attendees: list[str] | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    text: str | None = None,
    html: str | None = None,
    save_to_sent: bool | None = None,
    confirm: str | None = None,
    account: str | None = None,
    **_: Any,
) -> dict:
    _check_send_gate(confirm)
    a = get_account(_accounts(), account)
    result = smtp_ops.send_invite(
        a, to, subject, start, end, description, location, organizer, attendees,
        cc, bcc, text, html,
    )
    raw = result.pop("raw")
    save = a.save_to_sent if save_to_sent is None else save_to_sent
    saved: dict | None = None
    if save:
        try:
            saved = imap_ops.append_message(a, a.sent_folder, raw, "\\Seen")
        except Exception as e:
            saved = {"ok": False, "error": str(e)}
    return {"account": a.name, "sent": result, "saved_to_sent": saved}


@mcp.tool()
def email(action: str = "help", params: dict | None = None) -> dict:
    """Unified email tool for IMAP/SMTP accounts.

    First call: `action='help'` (no params) to see every action and its
    signature — this keeps the tool schema small so the client only loads
    parameter details when needed.

    Args:
        action: One of `help`, `list_accounts`, `list_folders`, `list_recent`,
            `search`, `read`, `download_attachments`, `send`, `save_draft`,
            `send_invite`.
        params: Action-specific parameters. See `action='help'` for each
            action's exact signature.
    """
    if action not in ACTIONS:
        return {
            "error": f"unknown action {action!r}",
            "available": list(ACTIONS.keys()),
            "hint": "call action='help' for parameter signatures",
        }
    handler, _desc = ACTIONS[action]
    kwargs = dict(params or {})
    try:
        return handler(**kwargs)
    except TypeError as e:
        return {
            "error": f"invalid params for {action!r}: {e}",
            "hint": "call action='help' to see the signature",
        }
    except PermissionError as e:
        return {"error": str(e), "action": action}
    except Exception as e:
        return {"error": str(e), "action": action, "type": type(e).__name__}


class BearerAuthASGI:
    """ASGI middleware that requires `Authorization: Bearer <token>` on every
    HTTP request except `/health`. Uses constant-time comparison."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in ("/health", "/healthz"):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})
            return
        auth = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v
                break
        if not hmac.compare_digest(auth, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="mcp"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def serve() -> None:
    """Dispatch to the transport chosen by `MCP_TRANSPORT` (default `stdio`).

    HTTP mode env vars:
      MCP_TRANSPORT       stdio | http | streamable-http | sse   (default stdio)
      MCP_HOST            bind host                              (default 127.0.0.1)
      MCP_PORT            bind port                              (default 8080)
      MCP_AUTH_TOKEN      bearer token — REQUIRED in http mode
      MCP_ALLOW_NO_AUTH   set truthy to skip token check (dev only)
    """
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    allow_no_auth = _bool_env("MCP_ALLOW_NO_AUTH")

    if not token and not allow_no_auth:
        raise SystemExit(
            "MCP HTTP mode requires MCP_AUTH_TOKEN. "
            "Set it to a random secret, or set MCP_ALLOW_NO_AUTH=1 to run without auth (not recommended)."
        )

    import uvicorn

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    if token:
        app = BearerAuthASGI(app, token)
        log.info("bearer auth ENABLED")
    else:
        log.warning("bearer auth DISABLED via MCP_ALLOW_NO_AUTH — do not expose this port publicly")

    log.info("cpanel-mail-mcp listening on http://%s:%s (transport=%s)", host, port, transport)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
