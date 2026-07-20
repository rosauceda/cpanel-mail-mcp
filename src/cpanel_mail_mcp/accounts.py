"""Account discovery from environment.

Priority:
  1. `EMAIL_ACCOUNTS_JSON`  — JSON array of account dicts.
  2. `EMAIL_ACCOUNTS_FILE`  — path to a JSON file with the same array.
  3. Legacy `CPANEL_USER`/`CPANEL_PASS`/... single account.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Account:
    name: str
    user: str
    password: str
    smtp_host: str
    smtp_port: int
    imap_host: str
    imap_port: int
    sent_folder: str
    drafts_folder: str
    save_to_sent: bool
    from_name: str | None
    # SSO identities that map to this account. Used only in CF Access OIDC
    # mode: the JWT's email claim is looked up here first, then falls back
    # to matching against `user`. Empty tuple = only `user` matches.
    sso_emails: tuple[str, ...] = ()


def _from_dict(d: dict) -> Account:
    if not d.get("user") or not d.get("password"):
        raise ValueError(f"account {d.get('name')!r} missing user/password")
    host = d.get("host") or ""
    raw_sso = d.get("sso_emails") or d.get("sso_email") or ()
    if isinstance(raw_sso, str):
        raw_sso = (raw_sso,)
    sso = tuple(e.strip().lower() for e in raw_sso if e and isinstance(e, str))
    return Account(
        name=str(d.get("name") or d["user"]),
        user=d["user"],
        password=d["password"],
        smtp_host=d.get("smtp_host") or host,
        smtp_port=int(d.get("smtp_port", 465)),
        imap_host=d.get("imap_host") or host,
        imap_port=int(d.get("imap_port", 993)),
        sent_folder=d.get("sent_folder", "Sent"),
        drafts_folder=d.get("drafts_folder", "Drafts"),
        save_to_sent=bool(d.get("save_to_sent", True)),
        from_name=d.get("from_name"),
        sso_emails=sso,
    )


def _legacy() -> Account | None:
    user = os.environ.get("CPANEL_USER")
    pw = os.environ.get("CPANEL_PASS")
    if not user or not pw:
        return None
    return _from_dict(
        {
            "name": os.environ.get("CPANEL_ACCOUNT_NAME", "default"),
            "user": user,
            "password": pw,
            "smtp_host": os.environ.get("CPANEL_SMTP_HOST", ""),
            "smtp_port": int(os.environ.get("CPANEL_SMTP_PORT", "465")),
            "imap_host": os.environ.get("CPANEL_IMAP_HOST", ""),
            "imap_port": int(os.environ.get("CPANEL_IMAP_PORT", "993")),
            "sent_folder": os.environ.get("CPANEL_SENT_FOLDER", "INBOX.Sent"),
            "drafts_folder": os.environ.get("CPANEL_DRAFTS_FOLDER", "INBOX.Drafts"),
            "save_to_sent": os.environ.get("CPANEL_SAVE_TO_SENT", "true").lower() != "false",
            "from_name": os.environ.get("CPANEL_FROM_NAME"),
        }
    )


def load_accounts() -> dict[str, Account]:
    accts: list[Account] = []
    raw = os.environ.get("EMAIL_ACCOUNTS_JSON")
    if raw:
        accts.extend(_from_dict(d) for d in json.loads(raw))
    else:
        path = os.environ.get("EMAIL_ACCOUNTS_FILE")
        if path:
            accts.extend(_from_dict(d) for d in json.loads(Path(path).read_text()))
    if not accts:
        legacy = _legacy()
        if legacy:
            accts.append(legacy)
    if not accts:
        raise RuntimeError(
            "No email accounts configured. Set EMAIL_ACCOUNTS_JSON, "
            "EMAIL_ACCOUNTS_FILE, or the legacy CPANEL_USER/CPANEL_PASS/... env vars."
        )
    out: dict[str, Account] = {}
    for a in accts:
        if a.name in out:
            raise ValueError(f"duplicate account name: {a.name!r}")
        out[a.name] = a
    return out


def get_account(accounts: dict[str, Account], name: str | None) -> Account:
    if not name:
        return next(iter(accounts.values()))
    if name in accounts:
        return accounts[name]
    raise ValueError(f"unknown account {name!r}. Available: {list(accounts)}")
