"""IMAP modified UTF-7 encoder/decoder roundtrips."""
from cpanel_mail_mcp import utf7


def test_ascii_roundtrip():
    for s in ["INBOX", "INBOX.Sent", "Notes", "hello world"]:
        assert utf7.decode(utf7.encode(s)) == s


def test_ampersand_escape():
    assert utf7.encode("A&B") == "A&-B"
    assert utf7.decode("A&-B") == "A&B"


def test_umlaut_roundtrip():
    for s in ["Entwürfe", "Postausgang", "Gelöscht"]:
        enc = utf7.encode(s)
        # non-ASCII always produces a `&…-` sequence somewhere
        is_pure_ascii = all(0x20 <= ord(c) <= 0x7e for c in s)
        assert is_pure_ascii or "&" in enc
        assert utf7.decode(enc) == s


def test_cjk_roundtrip():
    for s in ["下書き", "받은편지함", "已发送"]:
        assert utf7.decode(utf7.encode(s)) == s


def test_known_vectors():
    # Well-known Dovecot examples
    assert utf7.encode("Entwürfe") == "Entw&APw-rfe"
    assert utf7.decode("Entw&APw-rfe") == "Entwürfe"
