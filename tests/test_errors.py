"""Actionable-error dict shape."""
from cpanel_mail_mcp import errors


def test_folder_not_found_hint_mentions_list_folders():
    e = errors.FolderNotFound("Bogus", "acct")
    d = e.as_dict()
    assert d["code"] == "folder_not_found"
    assert "list_folders" in d["hint"]
    assert d["context"]["folder"] == "Bogus"


def test_message_not_found_hint_mentions_list_recent():
    e = errors.MessageNotFound("42", "INBOX")
    d = e.as_dict()
    assert "list_recent" in d["hint"]
    assert d["context"]["uid"] == "42"


def test_invalid_field_lists_allowed():
    e = errors.InvalidField("foo", ["FROM", "TO"])
    d = e.as_dict()
    assert "FROM" in d["hint"] and "TO" in d["hint"]
    assert d["context"]["allowed"] == ["FROM", "TO"]


def test_attachment_too_large_gives_mb():
    e = errors.AttachmentTooLarge(30_000_000, 25 * 1024 * 1024)
    d = e.as_dict()
    assert "25 MB" in d["hint"]
    assert d["context"]["actual_bytes"] == 30_000_000


def test_send_gate_blocked_mentions_env():
    e = errors.SendGateBlocked()
    d = e.as_dict()
    assert "EMAIL_SEND_CONFIRMATION_CODE" in d["hint"]


def test_rate_limited_includes_retry_after():
    e = errors.RateLimited(retry_after_s=42, bucket="send")
    d = e.as_dict()
    assert "42" in d["hint"]
    assert d["context"]["retry_after_seconds"] == 42


def test_unknown_account_lists_available():
    e = errors.UnknownAccount("nope", ["work", "home"])
    d = e.as_dict()
    assert "work" in d["hint"] and "home" in d["hint"]
