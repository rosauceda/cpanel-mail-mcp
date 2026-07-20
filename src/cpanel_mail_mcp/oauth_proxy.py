"""OAuth 2.1 Dynamic-Client-Registration proxy.

Cloudflare Access SaaS OIDC apps don't expose an RFC 7591 registration
endpoint, so MCP clients that require Dynamic Client Registration
(claude.ai Custom Connectors, Claude Desktop) can't self-register and
fail with "authorization failed" without even opening the OAuth browser.

This module lets the MCP server *look like* an OAuth 2.1 AS to the client:

* We advertise ourselves as the AS in the protected-resource metadata.
* We serve `.well-known/oauth-authorization-server` (RFC 8414) that
  composes CF's authorize/token/JWKS endpoints with our own `/register`
  endpoint (so DCR is "supported").
* We serve `.well-known/openid-configuration` too — some clients hit
  that URL by default.
* `POST /register` returns the pre-configured Client ID/Secret from env
  vars (`MCP_OAUTH_CLIENT_ID` + `MCP_OAUTH_CLIENT_SECRET`) regardless of
  what the client asked for. Both Claude and Cloudflare end up talking
  about the same static SaaS OIDC app.

The user still logs in through CF Access (Google, GitHub, OTP …); the
JWT that comes back is signed by CF and validated against CF's JWKS —
this module doesn't touch the token flow.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("cpanel_mail_mcp.oauth_proxy")


class OAuthProxy:
    def __init__(
        self,
        *,
        resource_url: str,
        upstream_issuer: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self.resource_url = resource_url.rstrip("/")
        self.upstream_issuer = upstream_issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._upstream_cfg: dict[str, Any] | None = None
        self._upstream_fetched_at: float = 0.0
        self._composed_cache: bytes | None = None

    def _fetch_upstream(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._upstream_cfg and now - self._upstream_fetched_at < 3600:
            return self._upstream_cfg
        url = f"{self.upstream_issuer}/.well-known/openid-configuration"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.load(r)
        except (urllib.error.URLError, ValueError) as e:
            log.error("OAuth proxy: cannot fetch upstream config at %s: %s", url, e)
            raise
        self._upstream_cfg = data
        self._upstream_fetched_at = now
        self._composed_cache = None  # invalidate composed metadata cache
        return data

    def composed_metadata(self) -> bytes:
        """Return the AS metadata JSON we advertise to MCP clients."""
        if self._composed_cache is not None:
            return self._composed_cache
        u = self._fetch_upstream()
        meta = {
            # Advertise ourselves as the AS — clients will PKCE + register with us,
            # then get redirected to CF for the actual login.
            "issuer": self.resource_url,
            "authorization_endpoint": u.get("authorization_endpoint"),
            "token_endpoint": u.get("token_endpoint"),
            "jwks_uri": u.get("jwks_uri"),
            "userinfo_endpoint": u.get("userinfo_endpoint"),
            "registration_endpoint": f"{self.resource_url}/register",
            "scopes_supported": u.get("scopes_supported", ["openid", "email", "profile"]),
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": u.get("response_types_supported", ["code"]),
            "response_modes_supported": u.get("response_modes_supported", ["query", "fragment"]),
            "token_endpoint_auth_methods_supported": u.get(
                "token_endpoint_auth_methods",
                ["client_secret_basic", "client_secret_post"],
            ),
            "id_token_signing_alg_values_supported": u.get(
                "id_token_signing_alg_values_supported", ["RS256"]
            ),
            "subject_types_supported": u.get("subject_types_supported", ["public"]),
            "code_challenge_methods_supported": ["S256"],
        }
        self._composed_cache = json.dumps(meta).encode()
        return self._composed_cache

    async def handle_register(self, scope, receive, send) -> None:
        """RFC 7591 Dynamic Client Registration — return static creds."""
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        try:
            req = json.loads(body) if body else {}
            if not isinstance(req, dict):
                req = {}
        except json.JSONDecodeError:
            req = {}

        # Echo back whatever the client asked for; overlay our real creds.
        response = {
            **req,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        body_out = json.dumps(response).encode()
        log.info(
            "DCR: returning static client_id=%s… for redirect_uris=%s",
            self.client_id[:10],
            req.get("redirect_uris"),
        )
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_out})


def from_env(resource_url: str) -> OAuthProxy | None:
    """Enable the proxy iff the three MCP_OAUTH_* vars are set."""
    upstream = os.environ.get("MCP_OAUTH_UPSTREAM_ISSUER", "").strip()
    cid = os.environ.get("MCP_OAUTH_CLIENT_ID", "").strip()
    sec = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "").strip()
    if not (upstream and cid and sec):
        return None
    if not resource_url:
        log.warning("OAuth proxy enabled but MCP_RESOURCE_URL is empty — proxy skipped")
        return None
    return OAuthProxy(
        resource_url=resource_url,
        upstream_issuer=upstream,
        client_id=cid,
        client_secret=sec,
    )
