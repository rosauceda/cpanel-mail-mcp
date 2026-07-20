"""Build an ICS (iCalendar) VEVENT body for meeting invites."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _parse_when(s: str) -> str:
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
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def build_ics(
    subject: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer: str = "",
    attendees: list[str] | None = None,
    method: str = "REQUEST",
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
        f"DTSTART:{_parse_when(start)}",
        f"DTEND:{_parse_when(end)}",
        f"SUMMARY:{_escape(subject)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if organizer:
        lines.append(f"ORGANIZER;CN={_escape(organizer)}:mailto:{organizer}")
    for att in attendees or []:
        lines.append(
            "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
            f"RSVP=TRUE;CN={_escape(att)}:mailto:{att}"
        )
    lines += ["STATUS:CONFIRMED", "SEQUENCE:0", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
