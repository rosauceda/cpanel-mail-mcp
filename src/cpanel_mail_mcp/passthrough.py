"""Per-request mailbox credentials (`MCP_AUTH_MODE=credentials`).

The client sends the mailbox login with every request and the server keeps
no passwords at rest — made for n8n's MCP Client Tool ("Multiple Headers
Auth"), where each n8n credential is one mailbox:

    X-Email-User:     juan@dominio.com
    X-Email-Password: ••••••••

`Authorization: Basic base64(user:password)` works too.

IMAP/SMTP hosts, ports and folders come from the server's environment (the
same `CPANEL_*` variables as the single-account setup, minus user/password),
so a caller can only try logins against *your* mail server. Optionally
restrict which address domains may log in with `MCP_ALLOWED_EMAIL_DOMAINS`.

A login is verified with IMAP once and cached (hash of user+password) for
`MCP_CREDENTIAL_CACHE_SECONDS`. Failed logins are throttled per mailbox and
globally, so the endpoint can't be used to guess passwords.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import imaplib
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .accounts import Account

HEADER_USER = b"x-email-user"
HEADER_PASSWORD = b"x-email-password"


class InvalidCredentials(Exception):
    pass


class DomainNotAllowed(Exception):
    pass


class UpstreamUnavailable(Exception):
    pass


class Throttled(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"too many failed logins; retry in {retry_after}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class MailTemplate:
    """Everything about an account except who logs in."""
    imap_host: str
    smtp_host: str
    imap_port: int = 993
    smtp_port: int = 465
    sent_folder: str = "INBOX.Sent"
    drafts_folder: str = "INBOX.Drafts"
    trash_folder: str | None = None
    save_to_sent: bool = True
    allowed_domains: tuple[str, ...] = ()

    def account(self, user: str, password: str) -> Account:
        return Account(
            name=user, user=user, password=password,
            smtp_host=self.smtp_host, smtp_port=self.smtp_port,
            imap_host=self.imap_host, imap_port=self.imap_port,
            sent_folder=self.sent_folder, drafts_folder=self.drafts_folder,
            save_to_sent=self.save_to_sent, from_name=None,
            trash_folder=self.trash_folder,
        )


def template_from_env() -> MailTemplate:
    env = os.environ.get
    host = env("CPANEL_HOST", "").strip()
    imap_host = env("CPANEL_IMAP_HOST", "").strip() or host
    smtp_host = env("CPANEL_SMTP_HOST", "").strip() or host
    if not imap_host or not smtp_host:
        raise SystemExit(
            "MCP_AUTH_MODE=credentials needs the mail server: set CPANEL_HOST "
            "(or CPANEL_IMAP_HOST + CPANEL_SMTP_HOST)."
        )
    domains = tuple(
        d.strip().lower().lstrip("@")
        for d in env("MCP_ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()
    )
    return MailTemplate(
        imap_host=imap_host,
        smtp_host=smtp_host,
        imap_port=int(env("CPANEL_IMAP_PORT", "993")),
        smtp_port=int(env("CPANEL_SMTP_PORT", "465")),
        sent_folder=env("CPANEL_SENT_FOLDER", "INBOX.Sent"),
        drafts_folder=env("CPANEL_DRAFTS_FOLDER", "INBOX.Drafts"),
        trash_folder=env("CPANEL_TRASH_FOLDER") or None,
        save_to_sent=env("CPANEL_SAVE_TO_SENT", "true").lower() != "false",
        allowed_domains=domains,
    )


def extract_credentials(headers: list[tuple[bytes, bytes]]) -> tuple[str, str] | None:
    """(user, password) from X-Email-User/X-Email-Password or Basic auth."""
    user = password = None
    basic = None
    for k, v in headers:
        k = k.lower()
        if k == HEADER_USER:
            user = v.decode("utf-8", "replace").strip()
        elif k == HEADER_PASSWORD:
            password = v.decode("utf-8", "replace")
        elif k == b"authorization" and v[:6].lower() == b"basic ":
            basic = v[6:].strip()
    if user and password:
        return user, password
    if basic:
        try:
            decoded = base64.b64decode(basic, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        u, sep, p = decoded.partition(":")
        if sep and u and p:
            return u.strip(), p
    return None


def _default_verify(account: Account) -> None:
    from . import imap_ops
    try:
        m = imap_ops._connect(account)
    except imaplib.IMAP4.error as e:  # LOGIN rejected
        raise InvalidCredentials(str(e)) from e
    except OSError as e:  # DNS, refused, timeout, TLS
        raise UpstreamUnavailable(str(e)) from e
    try:
        m.logout()
    except Exception:
        pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class CredentialVerifier:
    def __init__(
        self,
        template: MailTemplate,
        *,
        verify: Callable[[Account], None] = _default_verify,
        cache_seconds: int | None = None,
        max_failures_per_user: int = 5,
        max_failures_global: int = 50,
        window_seconds: int = 900,
    ) -> None:
        self.template = template
        self._verify = verify
        self.cache_seconds = (
            _int_env("MCP_CREDENTIAL_CACHE_SECONDS", 600) if cache_seconds is None else cache_seconds
        )
        self.max_user = max_failures_per_user
        self.max_global = max_failures_global
        self.window = window_seconds
        self._lock = threading.Lock()
        self._ok: dict[bytes, tuple[float, Account]] = {}
        self._fail_user: dict[str, deque[float]] = {}
        self._fail_global: deque[float] = deque()

    @staticmethod
    def _key(user: str, password: str) -> bytes:
        return hashlib.sha256(f"{user}\0{password}".encode()).digest()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] < now - self.window:
            q.popleft()

    def _retry_after(self, q: deque[float], now: float) -> int:
        return max(1, int(q[0] + self.window - now))

    def check(self, user: str, password: str) -> Account:
        """Return the caller's Account or raise. Blocking (may do an IMAP login)."""
        user = user.strip().lower()
        if "@" not in user or len(user) > 254 or any(c in user for c in "\r\n\0 "):
            raise InvalidCredentials("user must be an email address")
        if not password or len(password) > 1024:
            raise InvalidCredentials("empty or oversized password")
        domain = user.rsplit("@", 1)[1]
        if self.template.allowed_domains and domain not in self.template.allowed_domains:
            raise DomainNotAllowed(domain)

        key = self._key(user, password)
        now = time.monotonic()
        with self._lock:
            hit = self._ok.get(key)
            if hit and hit[0] > now:
                return hit[1]
            uq = self._fail_user.setdefault(user, deque())
            self._prune(uq, now)
            self._prune(self._fail_global, now)
            if len(uq) >= self.max_user:
                raise Throttled(self._retry_after(uq, now))
            if len(self._fail_global) >= self.max_global:
                raise Throttled(self._retry_after(self._fail_global, now))

        account = self.template.account(user, password)
        try:
            self._verify(account)
        except InvalidCredentials:
            with self._lock:
                t = time.monotonic()
                self._fail_user.setdefault(user, deque()).append(t)
                self._fail_global.append(t)
            raise

        with self._lock:
            self._ok[key] = (time.monotonic() + self.cache_seconds, account)
            self._fail_user.pop(user, None)
            if len(self._ok) > 10_000:  # drop expired entries
                t = time.monotonic()
                self._ok = {k: v for k, v in self._ok.items() if v[0] > t}
        return account
