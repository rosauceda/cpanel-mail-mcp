"""Verify Cloudflare Access JWTs (RS256, signed by CF Access team keys).

CF Access sits in front of the server and, after authenticating the caller,
injects a signed JWT either in `Cf-Access-Jwt-Assertion` (user SSO flow) or
in `Authorization: Bearer …` (OIDC SaaS app flow used by MCP clients).

The `email` claim in the JWT identifies the caller. We match it against
`Account.user` in `users.json` to pick the right mailbox.

Docs: https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/
"""
from __future__ import annotations

import os
from typing import Any

import jwt
from jwt import PyJWKClient


class CFAccessDisabled(Exception):
    """Raised when the CF Access env vars are missing — auth still works via bearer."""


class CFAccessInvalid(Exception):
    """Raised when a JWT is present but doesn't validate."""


class CFAccessVerifier:
    def __init__(self, team_domain: str, audience: str) -> None:
        team_domain = team_domain.strip().rstrip("/")
        if team_domain.startswith("https://"):
            team_domain = team_domain[len("https://") :]
        self.team_domain = team_domain
        self.audience = audience.strip()
        self.issuer = f"https://{self.team_domain}"
        self.jwks_url = f"{self.issuer}/cdn-cgi/access/certs"
        # PyJWKClient caches keys in-process; 1h lifespan matches CF's rotation cadence
        self._jwks = PyJWKClient(self.jwks_url, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss"]},
            )
            return claims
        except (jwt.PyJWTError, jwt.InvalidTokenError) as e:
            raise CFAccessInvalid(str(e)) from e


def from_env() -> CFAccessVerifier | None:
    """Build a verifier from `CF_ACCESS_TEAM_DOMAIN` + `CF_ACCESS_AUD` env vars.

    Returns None if either is unset — server keeps working with bearer-only auth.
    """
    team = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip()
    aud = os.environ.get("CF_ACCESS_AUD", "").strip()
    if not team or not aud:
        return None
    return CFAccessVerifier(team, aud)


def extract_email(claims: dict[str, Any]) -> str | None:
    """Pull the caller's email from the JWT claims.

    User SSO tokens have `email`. Service Token tokens have `common_name` like
    `mytoken.access` — we don't map those to a user; only user SSO can pick an
    account. If your setup needs service-token-only clients, add a mapping.
    """
    email = claims.get("email")
    if isinstance(email, str) and "@" in email:
        return email.lower()
    return None
