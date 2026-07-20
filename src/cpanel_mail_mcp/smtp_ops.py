"""SMTP send with attachments and calendar invites. Returns the raw bytes
so callers can APPEND them to Sent via IMAP."""
from __future__ import annotations

import base64
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from .accounts import Account
from .ics import build_ics


def _connect(a: Account) -> smtplib.SMTP | smtplib.SMTP_SSL:
    ctx = ssl.create_default_context()
    if a.smtp_port == 465:
        s: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
            a.smtp_host, a.smtp_port, context=ctx, timeout=30
        )
    else:
        s = smtplib.SMTP(a.smtp_host, a.smtp_port, timeout=30)
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
    s.login(a.user, a.password)
    return s


def _attach(msg: EmailMessage, att: dict) -> None:
    name = att.get("name") or att.get("filename")
    if "path" in att:
        p = Path(str(att["path"])).expanduser()
        if not p.is_file():
            raise ValueError(f"attachment path not found: {p}")
        data = p.read_bytes()
        name = name or p.name
    elif "content_base64" in att:
        data = base64.b64decode(att["content_base64"])
    elif "content" in att:
        raw = att["content"]
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    else:
        raise ValueError("attachment needs one of: 'path', 'content_base64', 'content'")
    if not name:
        raise ValueError("attachment missing 'name'")
    mime = att.get("mime") or att.get("mime_type")
    if not mime:
        guess, _ = mimetypes.guess_type(name)
        mime = guess or "application/octet-stream"
    maintype, _, subtype = mime.partition("/")
    msg.add_attachment(
        data,
        maintype=maintype or "application",
        subtype=subtype or "octet-stream",
        filename=name,
    )


def _from_header(a: Account) -> str:
    return formataddr((a.from_name, a.user)) if a.from_name else a.user


def _recipients(to: str, cc: str | None, bcc: str | None) -> list[str]:
    r = [x.strip() for x in to.split(",") if x.strip()]
    if cc:
        r += [x.strip() for x in cc.split(",") if x.strip()]
    if bcc:
        r += [x.strip() for x in bcc.split(",") if x.strip()]
    return r


def build_message(
    a: Account,
    to: str,
    subject: str,
    text: str | None,
    html: str | None,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to: str | None = None,
    attachments: list[dict] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _from_header(a)
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject or ""
    if html and text:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content("This message requires an HTML-capable client.")
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text or "")
    for att in attachments or []:
        _attach(msg, att)
    return msg


def send(
    a: Account,
    to: str,
    subject: str = "",
    text: str | None = None,
    html: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    msg = build_message(a, to, subject, text, html, cc, bcc, reply_to, attachments)
    recipients = _recipients(to, cc, bcc)
    with _connect(a) as s:
        s.send_message(msg, to_addrs=recipients)
    return {"ok": True, "recipients": recipients, "raw": msg.as_bytes()}


def send_invite(
    a: Account,
    to: str,
    subject: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer: str | None = None,
    attendees: list[str] | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    text: str | None = None,
    html: str | None = None,
) -> dict:
    to_list = [x.strip() for x in to.split(",") if x.strip()]
    ics = build_ics(
        subject=subject,
        start=start,
        end=end,
        description=description,
        location=location,
        organizer=organizer or a.user,
        attendees=attendees or to_list,
    )

    msg = EmailMessage()
    msg["From"] = _from_header(a)
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject

    plain = text or (
        f"{subject}\n\nWhen: {start} — {end}\n"
        + (f"Where: {location}\n" if location else "")
        + (f"\n{description}" if description else "")
    )
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
    msg.add_alternative(ics, subtype="calendar")
    for part in msg.iter_parts():
        if part.get_content_type() == "text/calendar":
            part.set_param("method", "REQUEST")
            part.set_param("charset", "UTF-8")
            part.set_param("name", "invite.ics")
            break
    msg.add_attachment(
        ics.encode("utf-8"),
        maintype="application",
        subtype="ics",
        filename="invite.ics",
    )

    recipients = _recipients(to, cc, bcc)
    with _connect(a) as s:
        s.send_message(msg, to_addrs=recipients)
    return {"ok": True, "recipients": recipients, "raw": msg.as_bytes()}
