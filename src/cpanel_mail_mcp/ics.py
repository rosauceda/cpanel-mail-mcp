"""Build an ICS (iCalendar) VEVENT body for meeting invites."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from email.utils import getaddresses
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone(name: str | None):
    """Zone for naive datetimes: explicit arg → MCP_DEFAULT_TIMEZONE → UTC."""
    name = (name or os.environ.get("MCP_DEFAULT_TIMEZONE", "")).strip()
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {name!r} (use an IANA name like 'America/Mexico_City')") from e


def _parse_when(s: str, tz: str | None = None) -> str:
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unrecognized datetime: {s!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz))
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _param(s: str) -> str:
    """Parameter value (e.g. CN): DQUOTE-wrapped, no DQUOTE/control chars inside."""
    clean = "".join(c for c in s if c >= " " and c != '"')
    return f'"{clean}"'


def _fold(line: str) -> str:
    """RFC 5545 §3.1: lines longer than 75 octets are folded with CRLF + space,
    never splitting a UTF-8 sequence."""
    out: list[str] = []
    cur, size, limit = "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def _people(values: list[str]) -> list[tuple[str, str]]:
    """`["Ana <a@x.com>, b@y.com"]` → [("Ana", "a@x.com"), ("", "b@y.com")]."""
    return [(n, addr) for n, addr in getaddresses(values) if addr and "@" in addr]


def build_ics(
    subject: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer: str = "",
    attendees: list[str] | None = None,
    method: str = "REQUEST",
    tz: str | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"{uuid.uuid4()}@cpanel-mail-mcp"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//cpanel-mail-mcp//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_parse_when(start, tz)}",
        f"DTEND:{_parse_when(end, tz)}",
        f"SUMMARY:{_escape(subject)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    for name, addr in _people([organizer])[:1] if organizer else []:
        lines.append(f"ORGANIZER;CN={_param(name or addr)}:mailto:{addr}")
    for name, addr in _people(attendees or []):
        lines.append(
            "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
            f"RSVP=TRUE;CN={_param(name or addr)}:mailto:{addr}"
        )
    lines += ["STATUS:CONFIRMED", "SEQUENCE:0", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
