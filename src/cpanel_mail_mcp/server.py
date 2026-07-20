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
from mcp.server.transport_security import TransportSecuritySettings

from . import cf_access, imap_ops, smtp_ops, users as users_mod
from .accounts import Account, get_account, load_accounts


def _transport_security() -> TransportSecuritySettings | None:
    """Build the MCP transport-security settings from env vars.

    FastMCP defaults to allowing only `localhost`/`127.0.0.1` as the Host
    header (DNS-rebinding protection). Behind a reverse proxy like
    Cloudflare Tunnel, requests arrive with the public hostname, which
    triggers 421 Misdirected Request unless we allowlist it.

      MCP_ALLOWED_HOSTS       comma-separated allowlist of Host header values
      MCP_ALLOWED_ORIGINS     comma-separated allowlist of Origin header values
      MCP_DISABLE_DNS_REBINDING_PROTECTION  set truthy to turn off both checks
    """
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

mcp = FastMCP("cpanel-mail", transport_security=_transport_security())

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


async def _send_json(send, status: int, payload: bytes, extra_headers: list | None = None) -> None:
    headers = [(b"content-type", b"application/json")]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


_www_authenticate_header: bytes = b'Bearer realm="mcp"'


def _set_www_authenticate(resource_url: str) -> None:
    """Update the WWW-Authenticate header to point 401 responses at the
    protected-resource metadata URL (RFC 9728 §5.1). Some MCP clients
    (Claude Custom Connectors) need this hint to start the OAuth flow."""
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
    for acct in users.values():
        if acct.user.lower() == email.lower():
            return acct
    return None


class UnifiedAuthASGI:
    """ASGI middleware that authenticates every HTTP request. Order:

    1. `/health`, `/healthz`, `/.well-known/*`  → passthrough (no auth)
    2. `Cf-Access-Jwt-Assertion` header         → verify with CF Access JWKS
       or `Authorization: Bearer <jwt>` where the token looks like a JWT
       and CF Access is configured. Email claim → account.
    3. `Authorization: Bearer <opaque-token>`   → users.json lookup (legacy
       bearer path — still works so CLI installs keep functioning during
       migration to CF Access OIDC).
    4. otherwise                                → 401

    Single-tenant mode (no users_map): CF Access disabled, single_token wins.
    """

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
        if path in ("/health", "/healthz") or path.startswith("/.well-known/"):
            await self.app(scope, receive, send)
            return

        cf_jwt_header = b""
        auth = b""
        for k, v in scope.get("headers", []):
            if k == b"cf-access-jwt-assertion":
                cf_jwt_header = v
            elif k == b"authorization":
                auth = v

        # Path 2a: explicit CF JWT header (browser SSO flow)
        if self._cf and cf_jwt_header:
            try:
                claims = self._cf.verify(cf_jwt_header.decode(errors="replace").strip())
            except cf_access.CFAccessInvalid as e:
                log.info("CF JWT invalid: %s", e)
                await _send_401(send, "cf-access-jwt-invalid")
                return
            return await self._dispatch_with_claims(scope, receive, send, claims)

        # Path 2b: Bearer that looks like a JWT (SaaS OIDC flow → Claude client)
        if self._cf and auth.startswith(b"Bearer "):
            token = auth[7:].decode("ascii", errors="replace").strip()
            if token.count(".") == 2:  # heuristic: JWT header.payload.signature
                try:
                    claims = self._cf.verify(token)
                    return await self._dispatch_with_claims(scope, receive, send, claims)
                except cf_access.CFAccessInvalid:
                    pass  # fall through to opaque-token path

        # Path 3: multi-user opaque bearer (legacy — still supported)
        if self._users and auth.startswith(b"Bearer "):
            token = auth[7:].decode("ascii", errors="replace").strip()
            acct = self._users.get(token)
            if acct is not None:
                return await self._dispatch_with_account(scope, receive, send, acct)

        # Path: single-tenant
        if self._expected is not None and hmac.compare_digest(auth, self._expected):
            await self.app(scope, receive, send)
            return

        await _send_401(send)

    async def _dispatch_with_claims(self, scope, receive, send, claims: dict) -> None:
        email = cf_access.extract_email(claims)
        if not email:
            log.info("CF JWT has no email claim; claims=%s", list(claims.keys()))
            await _send_403(send, "cf-access-jwt-no-email")
            return
        acct = _user_by_email(self._users, email) if self._users else None
        if acct is None:
            log.info("CF JWT email %r not in users.json", email)
            await _send_403(send, f"unknown-user:{email}")
            return
        await self._dispatch_with_account(scope, receive, send, acct)

    async def _dispatch_with_account(self, scope, receive, send, acct: Account) -> None:
        log.debug("auth ok: %s", acct.user)
        reset = _current_account_var.set(acct)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_account_var.reset(reset)


class WellKnownASGI:
    """Serves `/.well-known/oauth-protected-resource` (RFC 9728) so MCP clients
    can discover the authorization server that issues our tokens.

    Wraps the downstream app: `/health`, `/healthz`, and `/.well-known/*` are
    handled here; everything else is forwarded.
    """

    def __init__(self, app, *, resource_url: str, authorization_servers: list[str]) -> None:
        self.app = app
        self.resource_url = resource_url
        self.authorization_servers = authorization_servers
        body = {
            "resource": resource_url,
            "authorization_servers": authorization_servers,
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
        if path in ("/health", "/healthz"):
            await _send_json(send, 200, b"ok", [(b"content-type", b"text/plain; charset=utf-8")])
            return
        # Both the root well-known URL and the path-suffixed variant
        # (`/.well-known/oauth-protected-resource/mcp`, RFC 9728 §3.3 — the
        # form recent MCP clients like Claude try first) return the same
        # metadata JSON.
        if path == "/.well-known/oauth-protected-resource" or path.startswith(
            "/.well-known/oauth-protected-resource/"
        ):
            await _send_json(send, 200, self._prm_body)
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

    cf_verifier = cf_access.from_env()
    if cf_verifier:
        log.info(
            "CF Access OIDC ENABLED: team=%s aud=%s… (JWKS %s)",
            cf_verifier.team_domain,
            cf_verifier.audience[:10],
            cf_verifier.jwks_url,
        )

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    if multi_user and users_map:
        app = UnifiedAuthASGI(app, users=users_map, cf_verifier=cf_verifier)
        log.info("multi-user mode: %d user(s) loaded from %s", len(users_map), users_mod.users_path())
    elif single_token:
        app = UnifiedAuthASGI(app, single_token=single_token, cf_verifier=cf_verifier)
        log.info("single-tenant mode: bearer auth ENABLED")
    else:
        log.warning("bearer auth DISABLED via MCP_ALLOW_NO_AUTH — do not expose this port publicly")

    # Wrap with the well-known layer so /health and /.well-known/* bypass auth.
    resource_url = os.environ.get("MCP_RESOURCE_URL", "").strip().rstrip("/")
    as_urls = [u.strip() for u in os.environ.get("MCP_OAUTH_AUTHORIZATION_SERVERS", "").split(",") if u.strip()]
    if resource_url:
        _set_www_authenticate(resource_url)
        app = WellKnownASGI(app, resource_url=resource_url, authorization_servers=as_urls)
        log.info(
            "OAuth protected-resource metadata at %s/.well-known/oauth-protected-resource "
            "(authorization_servers=%s)",
            resource_url,
            as_urls or "[]",
        )
    else:
        # still expose /health without a resource_url configured
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
