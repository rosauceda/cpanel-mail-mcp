"""Tool layer end to end (FastMCP.call_tool) over fake IMAP + fake SMTP."""
import asyncio
import email
import json
import time

import pytest

from cpanel_mail_mcp import imap_ops, server, smtp_ops

from .fake_imap import FakeIMAP, FakeServer


class FakeSMTP:
    def __init__(self, sink: list, delay: float = 0.0) -> None:
        self.sink, self.delay = sink, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, msg, to_addrs=None):
        time.sleep(self.delay)
        self.sink.append((msg, list(to_addrs or [])))


@pytest.fixture
def env(monkeypatch):
    s = FakeServer()
    s.add_folder("INBOX.Sent", special="\\Sent")
    s.add_folder("INBOX.Drafts", special="\\Drafts")
    s.add_folder("INBOX.Trash", special="\\Trash")
    sent: list = []
    monkeypatch.setattr(imap_ops, "_connect", lambda a: FakeIMAP(s))
    monkeypatch.setattr(smtp_ops, "_connect", lambda a: FakeSMTP(sent))
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", json.dumps([{
        "name": "me", "user": "me@ex.com", "password": "p", "host": "mail.ex.com",
        "sent_folder": "INBOX.Sent", "drafts_folder": "INBOX.Drafts",
    }]))
    monkeypatch.delenv("EMAIL_SEND_CONFIRMATION_CODE", raising=False)
    monkeypatch.setattr(server, "_accounts_cache", None)
    server.idempotency.store._entries.clear()
    smtp_ops.configure_path_attachments(True, None)
    return s, sent


def call(name: str, args: dict):
    async def run():
        return await server.mcp.call_tool(name, args)
    res = asyncio.run(run())
    return res[1] if isinstance(res, tuple) else res


def test_reply_is_threaded_goes_to_reply_to_and_skips_self(env):
    s, sent = env
    s.add("INBOX", (
        "From: Ana <ana@x.com>\nReply-To: Lista <lista@x.com>\n"
        'To: me@ex.com, "Pérez, Juan" <juan@x.com>\nCc: ME@ex.com, bob@x.com\n'
        "Subject: Presupuesto\nMessage-ID: <b@x>\nReferences: <a@x>\n\nhola\n"
    ), uid=50)
    out = call("reply_email", {"uid": "50", "text": "ok", "reply_all": True})
    msg, rcpts = sent[-1]
    assert msg["Subject"] == "Re: Presupuesto"
    assert msg["In-Reply-To"] == "<b@x>"
    assert msg["References"] == "<a@x> <b@x>"
    assert msg["To"] == "Lista <lista@x.com>"
    assert rcpts == ["lista@x.com", "juan@x.com", "bob@x.com"]
    assert "me@ex.com" not in [r.lower() for r in rcpts]
    assert out["message_id"] == msg["Message-ID"]
    assert out["saved_to_sent"]["ok"] is True


def test_forward_html_only_keeps_html(env):
    s, sent = env
    s.add("INBOX", "From: a@x.com\nSubject: News\nContent-Type: text/html\n\n<b>hi</b>\n", uid=8)
    call("forward_email", {"uid": 8, "to": "z@x.com", "text": "FYI"})
    msg, _ = sent[-1]
    assert msg["Subject"] == "Fwd: News"
    html = msg.get_body(("html",)).get_content()
    assert "<b>hi</b>" in html and "FYI" in html


def test_uid_range_rejected_by_schema(env):
    s, _ = env
    s.add("INBOX", "From: a@x.com\nSubject: keep\n\nx\n", uid=5)
    with pytest.raises(Exception, match="pattern|string"):
        call("delete_email", {"uid": "1:*", "permanent": True})
    assert s.uids("INBOX") == [5]


def test_error_hint_reaches_the_model(env):
    with pytest.raises(Exception) as ei:
        call("read_email", {"uid": "1", "folder": "Nope"})
    assert "Hint:" in str(ei.value) and "list_folders" in str(ei.value)


def test_soft_delete_tool_reports_trash(env):
    s, _ = env
    s.add("INBOX", "From: a@x.com\nSubject: bye\n\nx\n", uid=12)
    out = call("delete_email", {"uid": "12"})
    assert out["permanently_deleted"] is False and out["moved_to"] == "INBOX.Trash"
    assert s.uids("INBOX") == []


def test_idempotency_key_sends_once_even_when_concurrent(env, monkeypatch):
    _, sent = env
    monkeypatch.setattr(smtp_ops, "_connect", lambda a: FakeSMTP(sent, delay=0.3))

    async def both():
        args = {"to": "z@x.com", "subject": "s", "text": "b", "idempotency_key": "k1"}
        return await asyncio.gather(
            server.mcp.call_tool("send_email", args),
            server.mcp.call_tool("send_email", args),
        )

    results = [r[1] if isinstance(r, tuple) else r for r in asyncio.run(both())]
    assert len(sent) == 1
    assert sorted(r["idempotent_replay"] for r in results) == [False, True]


def test_tools_run_off_the_event_loop(env, monkeypatch):
    """A slow SMTP send must not block other tool calls."""
    _, sent = env
    monkeypatch.setattr(smtp_ops, "_connect", lambda a: FakeSMTP(sent, delay=0.5))

    async def race():
        t0 = time.monotonic()
        slow = asyncio.create_task(server.mcp.call_tool("send_email", {"to": "z@x.com", "text": "b"}))
        await asyncio.sleep(0.05)
        await server.mcp.call_tool("list_accounts", {})
        fast_done = time.monotonic() - t0
        await slow
        return fast_done

    assert asyncio.run(race()) < 0.3


def test_save_draft_has_date_and_draft_flag(env):
    s, _ = env
    out = call("save_draft", {"to": "z@x.com", "subject": "borrador", "text": "x"})
    assert out["ok"] is True
    draft = s.folders["INBOX.Drafts"].messages[-1]
    assert "\\Draft" in draft.flags
    assert email.message_from_bytes(draft.raw)["Date"]


def test_send_invite_has_message_id_and_date(env):
    _, sent = env
    out = call("send_invite", {"to": "a@x.com", "subject": "Kickoff",
                               "start": "2026-10-01 10:00", "end": "2026-10-01 11:00",
                               "timezone": "America/Mexico_City"})
    msg, _ = sent[-1]
    assert out["message_id"] and out["message_id"] == msg["Message-ID"]
    assert msg["Date"]
    ics = next(p for p in msg.walk() if p.get_content_type() == "text/calendar").get_content()
    assert "DTSTART:20261001T160000Z" in ics


def test_path_attachments_blocked_in_remote_mode(env, tmp_path):
    _, sent = env
    secret = tmp_path / "users.json"
    secret.write_text("[]")
    smtp_ops.configure_path_attachments(False, None)
    try:
        with pytest.raises(Exception) as ei:
            call("send_email", {"to": "z@x.com", "text": "b", "attachments": [{"path": str(secret)}]})
        assert "content_base64" in str(ei.value)
        assert sent == []
    finally:
        smtp_ops.configure_path_attachments(True, None)
