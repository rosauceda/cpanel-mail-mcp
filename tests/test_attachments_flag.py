"""`has_attachments` / `attachment_count` from BODYSTRUCTURE, and agreement
with what `read_email` lists."""
import email.policy
from email.message import EmailMessage

import pytest

from cpanel_mail_mcp import imap_ops
from cpanel_mail_mcp.accounts import Account

from .fake_imap import FakeIMAP, FakeServer

PDF = b"%PDF-1.7 fake"
PNG = b"\x89PNG fake"


def _acct() -> Account:
    return Account("x", "me@ex.com", "p", "s", 465, "i", 993, "Sent", "Drafts", True, None)


def _base(subject: str) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@x.com", "me@ex.com", subject
    m["Date"] = "Thu, 24 Sep 2026 10:00:00 -0700"
    return m


def plain():
    m = _base("plain")
    m.set_content("hola")
    return m, 0


def pdf_attached():
    m = _base("pdf")
    m.set_content("see attached")
    m.add_attachment(PDF, "application", "pdf", filename="factura.pdf")
    return m, 1


def two_files():
    m = _base("two")
    m.set_content("x")
    m.add_attachment(PDF, "application", "pdf", filename="a.pdf")
    m.add_attachment(b"a,b", "text", "csv", filename="b.csv")
    return m, 2


def signature_logo_only():
    """Outlook/Gmail style: inline disposition + filename + Content-ID."""
    m = _base("logo")
    m.set_content("hola")
    m.add_alternative('<p>hola <img src="cid:logo@x"></p>', subtype="html")
    m.get_payload()[1].add_related(PNG, "image", "png", cid="<logo@x>",
                                   filename="image001.png", disposition="inline")
    return m, 0


def logo_without_disposition():
    """Newsletter style: `Content-Type: image/png; name=...` + Content-ID only."""
    m = _base("newsletter")
    m.set_content("hola")
    m.add_alternative('<p><img src="cid:l2@x"></p>', subtype="html")
    m.get_payload()[1].add_related(PNG, "image", "png", cid="<l2@x>", params={"name": "logo.png"})
    img = next(p for p in m.walk() if p.get_content_maintype() == "image")
    del img["Content-Disposition"]
    return m, 0


def logo_plus_real_attachment():
    m, _ = signature_logo_only()
    m.add_attachment(PDF, "application", "pdf", filename="cotizacion.pdf")
    return m, 1


def apple_inline_pdf():
    m = _base("inline pdf")
    m.set_content("x")
    m.add_attachment(PDF, "application", "pdf", filename="contrato.pdf", disposition="inline")
    return m, 1


def forwarded_as_attachment():
    inner = _base("Reunión original")
    inner.set_content("cuerpo original")
    m = _base("fwd")
    m.set_content("te reenvío")
    m.add_attachment(inner)
    return m, 1


def bare_pdf():
    m = _base("bare")
    m.set_content(PDF, "application", "pdf")
    return m, 1


def non_ascii_filename():
    m = _base("acentos")
    m.set_content("x")
    m.add_attachment(PDF, "application", "pdf", filename="résumé.pdf")
    return m, 1


CASES = [plain, pdf_attached, two_files, signature_logo_only, logo_without_disposition,
         logo_plus_real_attachment, apple_inline_pdf, forwarded_as_attachment, bare_pdf,
         non_ascii_filename]


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(imap_ops, "_connect", lambda a: FakeIMAP(s))
    return s


@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_flag_matches_expected_and_read_email(server, case):
    msg, expected = case()
    uid = server.add("INBOX", msg.as_bytes(policy=email.policy.SMTP), uid=500)
    summary = imap_ops.list_recent(_acct(), "INBOX")["messages"][0]
    assert summary["uid"] == str(uid)
    assert summary["attachment_count"] == expected
    assert summary["has_attachments"] is (expected > 0)

    full = imap_ops.read_email(_acct(), str(uid), "INBOX")
    assert full["attachment_count"] == expected
    assert full["has_attachments"] is (expected > 0)
    assert sum(1 for a in full["attachments"] if not a["embedded"]) == expected


def test_embedded_logo_is_listed_but_not_counted(server):
    msg, _ = signature_logo_only()
    server.add("INBOX", msg.as_bytes(policy=email.policy.SMTP), uid=7)
    full = imap_ops.read_email(_acct(), "7", "INBOX")
    assert [(a["filename"], a["embedded"]) for a in full["attachments"]] == [("image001.png", True)]


def test_forwarded_email_downloads_as_eml(server):
    msg, _ = forwarded_as_attachment()
    server.add("INBOX", msg.as_bytes(policy=email.policy.SMTP), uid=8)
    atts = imap_ops.download_attachments(_acct(), "8", "INBOX")
    assert atts[0]["filename"] == "Reunión original.eml"
    assert atts[0]["mime"] == "message/rfc822"
    import base64
    assert b"cuerpo original" in base64.b64decode(atts[0]["content_base64"])


def test_search_and_thread_carry_the_flag(server):
    msg, _ = pdf_attached()
    msg["Message-ID"] = "<p@x>"
    server.add("INBOX", msg.as_bytes(policy=email.policy.SMTP), uid=9)
    assert imap_ops.search(_acct(), "pdf", "SUBJECT", "INBOX")[0]["has_attachments"] is True
    assert imap_ops.get_thread(_acct(), "9", "INBOX")["messages"][0]["attachment_count"] == 1


def test_server_without_bodystructure_reports_unknown(server, monkeypatch):
    msg, _ = pdf_attached()
    server.add("INBOX", msg.as_bytes(policy=email.policy.SMTP), uid=10)
    real = FakeIMAP._uid_fetch

    def no_bs(self, args, literal):
        if "BODYSTRUCTURE" in args[1].upper():
            return "NO", [b"BODYSTRUCTURE not supported"]
        return real(self, args, literal)

    monkeypatch.setattr(FakeIMAP, "_uid_fetch", no_bs)
    m = imap_ops.list_recent(_acct(), "INBOX")["messages"][0]
    assert m["has_attachments"] is None and m["attachment_count"] is None


def test_parses_real_dovecot_reply():
    # Shape captured from the production cPanel/Dovecot server.
    line = (b'1956 (UID 11467 BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") NIL NIL '
            b'"quoted-printable" 450 13 NIL NIL NIL NIL)("text" "html" ("charset" "utf-8") NIL NIL '
            b'"quoted-printable" 9294 234 NIL NIL NIL NIL) "alternative" ("boundary" "--_P2") NIL NIL NIL)'
            b'("application" "pdf" ("name" "resumen.pdf") NIL NIL "base64" 4432 NIL '
            b'("attachment" ("filename" "resumen.pdf")) NIL NIL)'
            b'("application" "pdf" ("name" "auditoria.pdf") NIL NIL "base64" 5188 NIL '
            b'("attachment" ("filename" "auditoria.pdf")) NIL NIL) "mixed" ("boundary" "--_P1") NIL NIL NIL))')
    tree = imap_ops._sexp(imap_ops._sexp_tokens([line]))
    fields = dict(zip(tree[1][::2], tree[1][1::2]))
    assert fields["UID"] == "11467"
    assert imap_ops._bs_count(fields["BODYSTRUCTURE"], top=True) == 2
