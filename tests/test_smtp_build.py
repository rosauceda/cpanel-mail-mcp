"""EmailMessage construction: attachment limits, HTML+text combos, headers."""
import base64
import pytest

from cpanel_mail_mcp.accounts import Account
from cpanel_mail_mcp import smtp_ops
from cpanel_mail_mcp.errors import AttachmentTooLarge


def _acct(**over):
    d = {
        "name": "x", "user": "me@ex.com", "password": "p",
        "smtp_host": "s.ex.com", "smtp_port": 465,
        "imap_host": "i.ex.com", "imap_port": 993,
        "sent_folder": "Sent", "drafts_folder": "Drafts",
        "save_to_sent": True, "from_name": None,
        "sso_emails": (),
    }
    d.update(over)
    return Account(**d)


def test_build_text_only():
    msg = smtp_ops.build_message(_acct(), "to@x.com", "s", "hello", None)
    assert msg["Subject"] == "s"
    assert msg["From"] == "me@ex.com"
    assert msg["To"] == "to@x.com"
    assert "hello" in msg.get_content()
    assert msg["Message-ID"]


def test_build_html_and_text_is_multipart_alt():
    msg = smtp_ops.build_message(_acct(), "to@x.com", "s", "plain", "<p>rich</p>")
    parts = [p.get_content_type() for p in msg.walk()]
    assert "multipart/alternative" in parts
    assert "text/plain" in parts
    assert "text/html" in parts


def test_from_name_formatted():
    msg = smtp_ops.build_message(_acct(from_name="Me!"), "t@x", "s", "b", None)
    assert msg["From"] == "Me! <me@ex.com>"


def test_reply_headers():
    msg = smtp_ops.build_message(
        _acct(), "t@x", "Re: s", "b", None,
        in_reply_to="<orig@x>", references="<a@x> <orig@x>",
    )
    assert msg["In-Reply-To"] == "<orig@x>"
    assert msg["References"] == "<a@x> <orig@x>"


def test_attachment_from_content_string():
    msg = smtp_ops.build_message(
        _acct(), "t@x", "s", "b", None,
        attachments=[{"name": "note.txt", "content": "hola"}],
    )
    filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert "note.txt" in filenames


def test_attachment_size_limit_enforced(monkeypatch):
    monkeypatch.setenv("MCP_MAX_ATTACHMENT_MB", "1")
    big = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()
    with pytest.raises(AttachmentTooLarge) as ei:
        smtp_ops.build_message(
            _acct(), "t@x", "s", "b", None,
            attachments=[{"name": "big.bin", "content_base64": big}],
        )
    assert ei.value.context["limit_bytes"] == 1024 * 1024


def test_attachment_from_path(tmp_path):
    p = tmp_path / "file.txt"
    p.write_text("contents")
    msg = smtp_ops.build_message(
        _acct(), "t@x", "s", "b", None,
        attachments=[{"path": str(p)}],
    )
    assert "file.txt" in [pp.get_filename() for pp in msg.walk() if pp.get_filename()]


def test_missing_content_raises():
    with pytest.raises(ValueError, match="path.*content"):
        smtp_ops.build_message(
            _acct(), "t@x", "s", "b", None,
            attachments=[{"name": "x"}],
        )
