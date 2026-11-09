"""Path-attachment policy, users.json writes/reloads, error text, recipients."""
import json
import os
import stat

import pytest

from cpanel_mail_mcp import errors, smtp_ops, users as users_mod
from cpanel_mail_mcp.accounts import Account
from cpanel_mail_mcp.errors import AttachmentPathNotAllowed


def _acct() -> Account:
    return Account("x", "me@ex.com", "p", "s", 465, "i", 993, "Sent", "Drafts", True, None)


@pytest.fixture(autouse=True)
def _restore_policy():
    yield
    smtp_ops.configure_path_attachments(True, None)


def test_path_attachments_disabled(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    smtp_ops.configure_path_attachments(False, None)
    with pytest.raises(AttachmentPathNotAllowed):
        smtp_ops.build_message(_acct(), "t@x", "s", "b", None, attachments=[{"path": str(f)}])


def test_path_attachments_confined_to_root(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    (root / "ok.txt").write_text("ok")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    (root / "link.txt").symlink_to(outside)
    smtp_ops.configure_path_attachments(True, str(root))

    msg = smtp_ops.build_message(_acct(), "t@x", "s", "b", None,
                                 attachments=[{"path": str(root / "ok.txt")}])
    assert [p.get_filename() for p in msg.iter_attachments()] == ["ok.txt"]
    for bad in (outside, root / ".." / "secret.txt", root / "link.txt"):
        with pytest.raises(AttachmentPathNotAllowed):
            smtp_ops.build_message(_acct(), "t@x", "s", "b", None, attachments=[{"path": str(bad)}])


def test_built_messages_have_date():
    assert smtp_ops.build_message(_acct(), "t@x", "s", "b", None)["Date"]


def test_recipients_parse_display_names_with_commas():
    r = smtp_ops._recipients('"Sauceda, Rodrigo" <r@x.com>, b@y.com', "C <c@z.com>", "b@y.com")
    assert r == ["r@x.com", "b@y.com", "c@z.com"]


def test_error_str_includes_hint_and_code():
    s = str(errors.FolderNotFound("X", "acct"))
    assert "Hint:" in s and "list_folders" in s and "folder_not_found" in s


def test_save_users_is_atomic_and_0600(tmp_path, monkeypatch):
    p = tmp_path / "users.json"
    monkeypatch.setenv("EMAIL_USERS_FILE", str(p))
    users_mod.save_users([{"token": "t" * 30, "account": {"user": "a@x.com", "password": "p"}}])
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert json.loads(p.read_text())[0]["token"] == "t" * 30
    assert [x.name for x in tmp_path.iterdir()] == ["users.json"]  # no temp leftovers


def test_users_store_reloads_on_change(tmp_path, monkeypatch):
    p = tmp_path / "users.json"
    monkeypatch.setenv("EMAIL_USERS_FILE", str(p))
    acct = {"user": "a@x.com", "password": "p", "host": "h"}
    users_mod.save_users([{"token": "old-token-xxxxxxxxxxxx", "account": acct}])
    store = users_mod.UsersStore(p)
    assert set(store.get()) == {"old-token-xxxxxxxxxxxx"}
    users_mod.save_users([{"token": "new-token-xxxxxxxxxxxx", "account": acct}])
    assert set(store.get()) == {"new-token-xxxxxxxxxxxx"}
    p.write_text("{broken")  # a bad edit keeps the last good map
    assert set(store.get()) == {"new-token-xxxxxxxxxxxx"}
    p.unlink()
    assert store.get() == {}
