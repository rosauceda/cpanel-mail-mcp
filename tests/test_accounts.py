"""Account loading from env vars + JSON files + sso_emails handling."""
import json
import os
from pathlib import Path

import pytest

from cpanel_mail_mcp import accounts


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("CPANEL_", "EMAIL_")):
            monkeypatch.delenv(k, raising=False)
    yield


def test_legacy_cpanel_env(monkeypatch):
    monkeypatch.setenv("CPANEL_USER", "me@example.com")
    monkeypatch.setenv("CPANEL_PASS", "secret")
    monkeypatch.setenv("CPANEL_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("CPANEL_SMTP_HOST", "mail.example.com")
    accts = accounts.load_accounts()
    assert list(accts) == ["default"]
    a = accts["default"]
    assert a.user == "me@example.com"
    assert a.password == "secret"
    assert a.smtp_port == 465
    assert a.sent_folder == "INBOX.Sent"


def test_missing_config_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="No email accounts"):
        accounts.load_accounts()


def test_email_accounts_json(monkeypatch):
    payload = [
        {"name": "work", "user": "w@x.com", "password": "p",
         "smtp_host": "s.x.com", "imap_host": "i.x.com"},
        {"name": "home", "user": "h@y.com", "password": "q",
         "host": "z.com"},
    ]
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", json.dumps(payload))
    accts = accounts.load_accounts()
    assert list(accts) == ["work", "home"]
    assert accts["home"].smtp_host == "z.com"
    assert accts["home"].imap_host == "z.com"


def test_email_accounts_file(monkeypatch, tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([{"name": "x", "user": "u@v.com", "password": "p",
                              "smtp_host": "s", "imap_host": "i"}]))
    monkeypatch.setenv("EMAIL_ACCOUNTS_FILE", str(p))
    accts = accounts.load_accounts()
    assert list(accts) == ["x"]


def test_sso_emails_normalization():
    a = accounts._from_dict({
        "user": "u@v.com", "password": "p",
        "smtp_host": "s", "imap_host": "i",
        "sso_emails": ["A@B.com", "  c@d.com  ", ""],
    })
    assert a.sso_emails == ("a@b.com", "c@d.com")


def test_sso_email_singular_accepted():
    a = accounts._from_dict({
        "user": "u@v.com", "password": "p",
        "smtp_host": "s", "imap_host": "i",
        "sso_email": "one@x.com",
    })
    assert a.sso_emails == ("one@x.com",)


def test_duplicate_account_name_rejected(monkeypatch):
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", json.dumps([
        {"name": "dup", "user": "a@x", "password": "p", "host": "h"},
        {"name": "dup", "user": "b@x", "password": "p", "host": "h"},
    ]))
    with pytest.raises(ValueError, match="duplicate"):
        accounts.load_accounts()


def test_get_account_default_returns_first():
    accts = {"a": accounts._from_dict({"user": "a@x", "password": "p", "host": "h", "name": "a"}),
             "b": accounts._from_dict({"user": "b@x", "password": "p", "host": "h", "name": "b"})}
    assert accounts.get_account(accts, None).name == "a"


def test_get_account_unknown_raises():
    accts = {"a": accounts._from_dict({"user": "a@x", "password": "p", "host": "h", "name": "a"})}
    with pytest.raises(ValueError, match="unknown"):
        accounts.get_account(accts, "nope")
