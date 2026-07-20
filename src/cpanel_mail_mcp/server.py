"""cpanel-mail-mcp — single MCP tool `email` with lazy action discovery.

Design: one tool, one `action` string, one `params` dict. The tool schema
stays tiny so it costs the client almost nothing in context; the client
calls `action='help'` to discover what each action wants.
"""
from __future__ import annotations

import contextvars
import hmac
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from . import imap_ops, smtp_ops, users as users_mod
from .accounts import Account, get_account, load_accounts

log = logging.getLogger("cpanel_mail_mcp")

# Set by the auth middleware in multi-user mode; unset in single-tenant mode.
_current_account_var: contextvars.ContextVar[Account | None] = contextvars.ContextVar(
    "cpanel_mail_current_account", default=None
)


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


def _pick_account(account_param: str | None) -> Account:
    """Return the account this request should use.

    Multi-user mode: the auth middleware set a ContextVar from the bearer
    token; that account wins and any `account` param is ignored (prevents
    one user from acting on another's mailbox).

    Single-tenant mode: fall back to the `account` param, or the first
    configured account.
    """
    forced = _current_account_var.get()
    if forced is not None:
        return forced
    return get_account(_accounts(), account_param)


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


@_action("list_accounts", "List account(s) available to this caller (names + hosts, no secrets).")
def _act_list_accounts(**_: Any) -> dict:
    # In multi-user mode the caller only ever sees their own account.
    forced = _current_account_var.get()
    accts = [forced] if forced is not None else list(_accounts().values())
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
            for a in accts
        ]
    }


@_action("list_folders", "List IMAP folders. Params: account?")
def _act_list_folders(account: str | None = None, **_: Any) -> dict:
    a = _pick_account(account)
    return {"account": a.name, "folders": imap_ops.list_folders(a)}


@_action(
    "list_recent",
    "List recent messages. Params: folder='INBOX', limit=20, account?",
)
def _act_list_recent(
    folder: str = "INBOX", limit: int = 20, account: str | None = None, **_: Any
) -> dict:
    a = _pick_account(account)
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
    a = _pick_account(account)
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
    a = _pick_account(account)
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
    a = _pick_account(account)
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
    a = _pick_account(account)
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
    a = _pick_account(account)
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
    a = _pick_account(account)
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


async def _send_401(send) -> None:
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


class BearerAuthASGI:
    """ASGI middleware that authenticates every HTTP request via
    `Authorization: Bearer <token>` (except `/health`, which is open).

    Two modes:
      - single-tenant: pass `single_token=<str>`. All requests must match it
        (constant-time compare).
      - multi-user:    pass `users={token: Account, ...}`. The middleware
        looks up the caller's account and binds it in a ContextVar so tool
        handlers act on that account instead of any `params.account`.
    """

    def __init__(
        self,
        app,
        single_token: str | None = None,
        users: dict[str, Account] | None = None,
    ) -> None:
        if bool(single_token) == bool(users):
            raise ValueError("BearerAuthASGI: pick exactly one of single_token or users")
        self.app = app
        self._expected = f"Bearer {single_token}".encode() if single_token else None
        self._users = users or {}

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
        # multi-user
        if self._users:
            if not auth.startswith(b"Bearer "):
                await _send_401(send)
                return
            token = auth[7:].decode("ascii", errors="replace").strip()
            acct = self._users.get(token)
            if acct is None:
                await _send_401(send)
                return
            log.debug("auth ok: %s", acct.user)
            reset = _current_account_var.set(acct)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_account_var.reset(reset)
            return
        # single-tenant
        if not hmac.compare_digest(auth, self._expected):  # type: ignore[arg-type]
            await _send_401(send)
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
      EMAIL_USERS_FILE    users.json path → multi-user mode (token → account)
      MCP_AUTH_TOKEN      single-tenant bearer token (ignored if EMAIL_USERS_FILE exists)
      MCP_ALLOW_NO_AUTH   skip token check (dev only)
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
                "HTTP mode needs auth. Either:\n"
                "  • set EMAIL_USERS_FILE to a users.json (multi-user mode), or\n"
                "  • set MCP_AUTH_TOKEN to a random secret (single-tenant), or\n"
                "  • set MCP_ALLOW_NO_AUTH=1 to run without auth (dev only)."
            )

    import uvicorn

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    if multi_user and users_map:
        app = BearerAuthASGI(app, users=users_map)
        log.info("multi-user mode: %d user(s) loaded from %s", len(users_map), users_mod.users_path())
    elif single_token:
        app = BearerAuthASGI(app, single_token=single_token)
        log.info("single-tenant mode: bearer auth ENABLED")
    else:
        log.warning("bearer auth DISABLED via MCP_ALLOW_NO_AUTH — do not expose this port publicly")

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
