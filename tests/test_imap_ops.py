"""imap_ops against an in-memory IMAP server whose UIDs ≠ sequence numbers."""
from email.header import Header

import pytest

from cpanel_mail_mcp import imap_ops
from cpanel_mail_mcp.accounts import Account
from cpanel_mail_mcp.errors import FolderNotFound, InvalidUid, MessageNotFound, TrashNotFound

from .fake_imap import FakeIMAP, FakeServer


def _acct(**over) -> Account:
    d = dict(name="x", user="me@ex.com", password="p", smtp_host="s", smtp_port=465,
             imap_host="i", imap_port=993, sent_folder="INBOX.Sent",
             drafts_folder="INBOX.Drafts", save_to_sent=True, from_name=None)
    d.update(over)
    return Account(**d)


def _mail(subject: str, frm: str = "a@x.com", msgid: str = "", extra: str = "",
          body: str = "hello") -> str:
    if not subject.isascii():  # real clients send RFC 2047 encoded-words
        subject = Header(subject, "utf-8").encode()
    hdr = f"From: {frm}\nTo: me@ex.com\nSubject: {subject}\nDate: Thu, 24 Sep 2026 10:00:00 -0700\n"
    if msgid:
        hdr += f"Message-ID: {msgid}\n"
    return hdr + extra + "\n" + body + "\n"


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(imap_ops, "_connect", lambda a: FakeIMAP(s))
    return s


def _seed(s: FakeServer) -> None:
    # UIDs 3, 7, 15 → sequence numbers 1, 2, 3. UID 3 exists *and* is a
    # different message than sequence 3 — the classic collision.
    s.add("INBOX", _mail("first"), uid=3)
    s.add("INBOX", _mail("second"), uid=7, flags={"\\Seen"})
    s.add("INBOX", _mail("third"), uid=15)


def test_list_recent_returns_real_uids_flags_and_pages(server):
    _seed(server)
    page = imap_ops.list_recent(_acct(), "INBOX", limit=2)
    assert [m["uid"] for m in page["messages"]] == ["15", "7"]
    assert [m["subject"] for m in page["messages"]] == ["third", "second"]
    assert page["messages"][1]["flags"] == ["\\Seen"]
    assert page["next_cursor"] == "7"
    older = imap_ops.list_recent(_acct(), "INBOX", limit=2, cursor=page["next_cursor"])
    assert [m["uid"] for m in older["messages"]] == ["3"]
    assert older["next_cursor"] is None


def test_read_paths_use_examine_and_never_store(server):
    _seed(server)
    imap_ops.list_recent(_acct(), "INBOX")
    imap_ops.read_email(_acct(), "15", "INBOX")
    imap_ops.search(_acct(), "third", "SUBJECT", "INBOX")
    cmds = server.commands
    assert ("EXAMINE", "INBOX") in cmds
    assert ("SELECT", "INBOX") not in cmds
    assert not any(c[:2] == ("UID", "STORE") for c in cmds)
    assert "\\Seen" not in server.get("INBOX", 15).flags


def test_fetch_parsing_when_flags_follow_the_literal(monkeypatch):
    s = FakeServer(flags_after_literal=True)
    monkeypatch.setattr(imap_ops, "_connect", lambda a: FakeIMAP(s))
    _seed(s)
    page = imap_ops.list_recent(_acct(), "INBOX", limit=3)
    assert [m["uid"] for m in page["messages"]] == ["15", "7", "3"]
    assert page["messages"][1]["flags"] == ["\\Seen"]


def test_search_non_ascii_and_quotes(server):
    server.add("INBOX", _mail("Reunión semanal"), uid=40)
    server.add("INBOX", _mail('He said "hi"'), uid=41)
    server.add("INBOX", _mail("otro"), uid=42)
    res = imap_ops.search(_acct(), "Reunión", "SUBJECT", "INBOX")
    assert [m["uid"] for m in res] == ["40"]
    res = imap_ops.search(_acct(), 'said "hi"', "SUBJECT", "INBOX")
    assert [m["uid"] for m in res] == ["41"]
    sent = [c for c in server.commands if c[:2] == ("UID", "SEARCH") and "CHARSET" in c]
    assert sent and sent[0][-1] == ("literal", "Reunión".encode())


def test_search_falls_back_to_ascii_when_charset_rejected(monkeypatch):
    s = FakeServer(utf8_search=False)
    monkeypatch.setattr(imap_ops, "_connect", lambda a: FakeIMAP(s))
    s.add("INBOX", _mail("plain subject"), uid=5)
    assert [m["uid"] for m in imap_ops.search(_acct(), "plain", "SUBJECT", "INBOX")] == ["5"]
    with pytest.raises(RuntimeError, match="UTF-8"):
        imap_ops.search(_acct(), "Reunión", "SUBJECT", "INBOX")


def test_read_email_threading_headers(server):
    server.add("INBOX", _mail("q", msgid="<b@x>", extra=(
        "In-Reply-To: <a@x>\nReferences: <root@x>\n <a@x>\nReply-To: Lists <list@x.com>\n"
    )), uid=9)
    r = imap_ops.read_email(_acct(), "9", "INBOX")
    assert r["message_id"] == "<b@x>"
    assert r["in_reply_to"] == "<a@x>"
    assert r["references"] == "<root@x> <a@x>"
    assert r["reply_to"] == "Lists <list@x.com>"


def test_single_part_html_is_body_html(server):
    server.add("INBOX", "From: a@x.com\nSubject: h\nContent-Type: text/html; charset=utf-8\n\n<p>hola</p>\n", uid=4)
    r = imap_ops.read_email(_acct(), "4", "INBOX")
    assert r["body_html"].strip() == "<p>hola</p>"
    assert r["body_text"] == ""


def test_move_targets_the_uid_not_the_sequence_number(server):
    _seed(server)
    server.add_folder("INBOX.Archivo", uidnext=100)
    r = imap_ops.move_message(_acct(), "3", "INBOX", "INBOX.Archivo")
    assert r["method"] == "MOVE"
    assert r["new_uid"] == "100"
    assert server.uids("INBOX") == [7, 15]
    moved = server.get("INBOX.Archivo", 100)
    assert b"Subject: first" in moved.raw


def test_move_fallback_expunges_only_that_message(monkeypatch):
    s = FakeServer(supports_move=False)
    monkeypatch.setattr(imap_ops, "_connect", lambda a: FakeIMAP(s))
    _seed(s)
    s.get("INBOX", 15).flags.add("\\Deleted")  # flagged by another client
    s.add_folder("INBOX.Archivo")
    r = imap_ops.move_message(_acct(), "7", "INBOX", "INBOX.Archivo")
    assert r["method"] == "COPY+EXPUNGE"
    assert s.uids("INBOX") == [3, 15]  # UID EXPUNGE left UID 15 alone


def test_move_to_missing_folder_is_folder_not_found(server):
    _seed(server)
    with pytest.raises(FolderNotFound):
        imap_ops.move_message(_acct(), "7", "INBOX", "INBOX.Nope")
    assert server.uids("INBOX") == [3, 7, 15]


def test_missing_uid_is_message_not_found(server):
    _seed(server)
    server.add_folder("INBOX.Archivo")
    with pytest.raises(MessageNotFound):
        imap_ops.move_message(_acct(), "2", "INBOX", "INBOX.Archivo")  # seq 2 exists, UID 2 doesn't


@pytest.mark.parametrize("bad", ["1:*", "1,2", "*", "0", "abc", "-1", " 7 7"])
def test_uid_ranges_and_garbage_are_rejected(server, bad):
    _seed(server)
    with pytest.raises(InvalidUid):
        imap_ops.delete_message(_acct(), bad, "INBOX", permanent=True)
    assert server.uids("INBOX") == [3, 7, 15]


def test_soft_delete_uses_special_use_trash(server):
    _seed(server)
    server.add_folder("INBOX.Papelera", special="\\Trash")
    r = imap_ops.delete_message(_acct(), "7", "INBOX")
    assert r["permanently_deleted"] is False
    assert r["moved_to"] == "INBOX.Papelera"
    assert server.uids("INBOX") == [3, 15]
    assert len(server.folders["INBOX.Papelera"].messages) == 1


def test_soft_delete_prefers_account_trash(server):
    _seed(server)
    server.add_folder("INBOX.Trash", special="\\Trash")
    server.add_folder("Borrados")
    r = imap_ops.delete_message(_acct(trash_folder="Borrados"), "7", "INBOX")
    assert r["moved_to"] == "Borrados"


def test_soft_delete_without_trash_never_hard_deletes(server):
    _seed(server)
    with pytest.raises(TrashNotFound):
        imap_ops.delete_message(_acct(), "7", "INBOX")
    with pytest.raises(FolderNotFound):
        imap_ops.delete_message(_acct(), "7", "INBOX", trash_folder="INBOX.Nope")
    assert server.uids("INBOX") == [3, 7, 15]


def test_permanent_delete_removes_only_that_uid(server):
    _seed(server)
    server.get("INBOX", 3).flags.add("\\Deleted")
    r = imap_ops.delete_message(_acct(), "15", "INBOX", permanent=True)
    assert r["permanently_deleted"] is True
    assert server.uids("INBOX") == [3, 7]


def test_flags_roundtrip(server):
    _seed(server)
    r = imap_ops.star(_acct(), "3", "INBOX")
    assert "\\Flagged" in r["flags_after"]
    r = imap_ops.mark_unread(_acct(), "7", "INBOX")
    assert "\\Seen" not in r["flags_after"]
    assert server.get("INBOX", 15).flags == set()


def test_get_thread_finds_ancestors_and_replies(server):
    server.add("INBOX", _mail("Reunión semanal", msgid="<a@x>"), uid=20)
    server.add("INBOX", _mail("Re: Reunión semanal", msgid="<b@x>",
                              extra="In-Reply-To: <a@x>\nReferences: <a@x>\n"), uid=21)
    server.add("INBOX", _mail("Re: Re: Reunión semanal", msgid="<c@x>",
                              extra="In-Reply-To: <b@x>\nReferences: <a@x> <b@x>\n"), uid=25)
    server.add("INBOX", _mail("otra cosa", msgid="<z@x>"), uid=30)
    for start in ("20", "21", "25"):
        t = imap_ops.get_thread(_acct(), start, "INBOX")
        assert [m["uid"] for m in t["messages"]] == ["20", "21", "25"], start


def test_trash_detection_by_common_name(server):
    _seed(server)
    server.add_folder("INBOX.Trash")  # no SPECIAL-USE flag
    r = imap_ops.delete_message(_acct(), "3", "INBOX")
    assert r["moved_to"] == "INBOX.Trash"
