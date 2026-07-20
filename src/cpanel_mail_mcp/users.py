"""Multi-user auth: `bearer token → Account` mapping.

The users file (path from `EMAIL_USERS_FILE`) is a JSON array:

    [
      {
        "token": "abc123...",
        "account": { ...same shape as an accounts.json entry... }
      },
      ...
    ]

When this file exists, the server runs in **multi-user mode**: the token
in `Authorization: Bearer …` identifies the caller and picks the account.
Callers cannot override the account by passing `params.account`.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from .accounts import Account, _from_dict


def users_path() -> Path | None:
    p = os.environ.get("EMAIL_USERS_FILE")
    return Path(p) if p else None


def is_multi_user() -> bool:
    p = users_path()
    return bool(p and p.is_file())


def load_users() -> dict[str, Account]:
    """Return `{token: Account}` for the currently configured users file."""
    p = users_path()
    if not p or not p.is_file():
        return {}
    data = json.loads(p.read_text() or "[]")
    if not isinstance(data, list):
        raise ValueError(f"{p} must contain a JSON array")
    out: dict[str, Account] = {}
    for i, entry in enumerate(data):
        token = entry.get("token")
        acct_raw = entry.get("account")
        if not token or not acct_raw:
            raise ValueError(f"{p}[{i}] missing 'token' or 'account'")
        if token in out:
            raise ValueError(f"{p}[{i}] duplicate token")
        out[token] = _from_dict(acct_raw)
    return out


def load_users_raw() -> list[dict]:
    p = users_path()
    if not p or not p.is_file():
        return []
    return json.loads(p.read_text() or "[]")


def save_users(users: list[dict]) -> None:
    p = users_path()
    if not p:
        raise SystemExit(
            "EMAIL_USERS_FILE not set. "
            "Point it to a path like /etc/cpanel-mail-mcp/users.json first."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(users, indent=2) + "\n")
    os.chmod(p, 0o600)


def new_token() -> str:
    return secrets.token_urlsafe(36)
