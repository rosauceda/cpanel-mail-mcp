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

The running server re-reads the file whenever it changes (`UsersStore`), so
`admin add-user` / `rotate-token` / `remove-user` take effect on the next
request without a restart.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path

from .accounts import Account, _from_dict

log = logging.getLogger("cpanel_mail_mcp.users")


def users_path() -> Path | None:
    p = os.environ.get("EMAIL_USERS_FILE")
    return Path(p) if p else None


def is_multi_user() -> bool:
    p = users_path()
    return bool(p and p.is_file())


def load_users(path: Path | None = None) -> dict[str, Account]:
    """Return `{token: Account}` for the configured (or given) users file."""
    p = path or users_path()
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
    """Atomically replace the users file. The temp file is created 0600 (never
    briefly world-readable) and keeps the previous file's owner, so a root
    admin editing it doesn't lock out the service user."""
    p = users_path()
    if not p:
        raise SystemExit(
            "EMAIL_USERS_FILE not set. "
            "Point it to a path like /etc/cpanel-mail-mcp/users.json first."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = p.stat() if p.exists() else None
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(users, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        if prev is not None:
            try:
                os.chown(tmp, prev.st_uid, prev.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class UsersStore:
    """`{token: Account}` view of the users file, reloaded when it changes.

    Checked with one `stat()` per request. If the file becomes unreadable or
    invalid, the last good map is kept and the error is logged; if it is
    deleted, every token is revoked.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._users: dict[str, Account] = {}
        self.get()

    def get(self) -> dict[str, Account]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            if self._users:
                log.warning("users file %s disappeared — all tokens revoked", self.path)
            self._users, self._sig = {}, None
            return self._users
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return self._users
        with self._lock:
            if sig != self._sig:
                try:
                    self._users = load_users(self.path)
                    log.info("loaded %d user(s) from %s", len(self._users), self.path)
                except Exception as e:
                    log.error("cannot reload %s (keeping previous users): %s", self.path, e)
                self._sig = sig
        return self._users


def new_token() -> str:
    return secrets.token_urlsafe(36)
