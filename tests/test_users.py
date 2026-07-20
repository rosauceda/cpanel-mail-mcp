"""users.json load/save + admin CLI subcommands."""
import json
import os

import pytest

from cpanel_mail_mcp import admin, users as users_mod


@pytest.fixture
def users_file(tmp_path, monkeypatch):
    p = tmp_path / "users.json"
    p.write_text("[]")
    monkeypatch.setenv("EMAIL_USERS_FILE", str(p))
    return p


def _run(argv):
    return admin.main(argv)


def test_is_multi_user(users_file):
    assert users_mod.is_multi_user()


def test_add_user_writes_token(users_file, capsys):
    rc = _run(["add-user", "--email", "juan@dom.com", "--password", "pw",
               "--host", "mail.dom.com"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "juan@dom.com" in out
    assert "bearer token" in out.lower()
    data = json.loads(users_file.read_text())
    assert len(data) == 1
    assert data[0]["account"]["user"] == "juan@dom.com"
    assert len(data[0]["token"]) > 20


def test_add_user_duplicate_fails(users_file):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h"])
    rc = _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h"])
    assert rc == 1


def test_list_users(users_file, capsys):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h"])
    _run(["add-user", "--email", "b@x.com", "--password", "p", "--host", "h"])
    capsys.readouterr()  # drain
    rc = _run(["list-users"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "a@x.com" in out
    assert "b@x.com" in out


def test_rotate_token(users_file, capsys):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h"])
    old = json.loads(users_file.read_text())[0]["token"]
    capsys.readouterr()
    rc = _run(["rotate-token", "--email", "a@x.com"])
    new = json.loads(users_file.read_text())[0]["token"]
    assert rc == 0
    assert old != new
    assert len(new) > 20


def test_remove_user(users_file):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h"])
    _run(["add-user", "--email", "b@x.com", "--password", "p", "--host", "h"])
    _run(["remove-user", "--email", "a@x.com"])
    data = json.loads(users_file.read_text())
    assert [e["account"]["user"] for e in data] == ["b@x.com"]


def test_add_sso_email(users_file):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h"])
    _run(["add-sso-email", "--email", "a@x.com", "--sso-email", "b@gmail.com",
          "--sso-email", "c@gmail.com"])
    data = json.loads(users_file.read_text())[0]["account"]
    assert data["sso_emails"] == ["b@gmail.com", "c@gmail.com"]


def test_remove_sso_email(users_file):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h",
          "--sso-email", "b@gmail.com", "--sso-email", "c@gmail.com"])
    _run(["remove-sso-email", "--email", "a@x.com", "--sso-email", "b@gmail.com"])
    data = json.loads(users_file.read_text())[0]["account"]
    assert data["sso_emails"] == ["c@gmail.com"]


def test_load_users_matches_json(users_file):
    _run(["add-user", "--email", "a@x.com", "--password", "p", "--host", "h",
          "--sso-email", "sso@a.com"])
    loaded = users_mod.load_users()
    assert len(loaded) == 1
    acct = next(iter(loaded.values()))
    assert acct.user == "a@x.com"
    assert acct.sso_emails == ("sso@a.com",)
