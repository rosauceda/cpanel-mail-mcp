"""ICS builder produces valid VCALENDAR/VEVENT content."""
import re
from datetime import datetime, timezone

from cpanel_mail_mcp import ics


def test_minimal_invite():
    body = ics.build_ics(
        subject="Kickoff", start="2026-07-25 09:00", end="2026-07-25 10:00",
    )
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.endswith("END:VCALENDAR\r\n")
    assert "METHOD:REQUEST" in body
    assert "BEGIN:VEVENT" in body
    assert "END:VEVENT" in body
    assert "SUMMARY:Kickoff" in body
    assert re.search(r"DTSTART:\d{8}T\d{6}Z", body)
    assert re.search(r"DTEND:\d{8}T\d{6}Z", body)


def test_utc_conversion_from_naive():
    body = ics.build_ics(subject="x", start="2026-07-25 09:00", end="2026-07-25 10:00")
    assert "DTSTART:20260725T090000Z" in body
    assert "DTEND:20260725T100000Z" in body


def test_iso8601_with_offset_converts_to_utc():
    body = ics.build_ics(
        subject="x",
        start="2026-07-25T09:00:00-06:00",
        end="2026-07-25T10:00:00-06:00",
    )
    assert "DTSTART:20260725T150000Z" in body  # 09:00 CDT → 15:00 UTC
    assert "DTEND:20260725T160000Z" in body


def test_description_escaped():
    body = ics.build_ics(subject="x", start="2026-07-25 09:00", end="2026-07-25 10:00",
                         description="line1\nline2, with; special\\chars")
    assert "DESCRIPTION:line1\\nline2\\, with\\; special\\\\chars" in body


def test_attendees_and_organizer():
    body = ics.build_ics(
        subject="x", start="2026-07-25 09:00", end="2026-07-25 10:00",
        organizer="me@example.com", attendees=["a@x.com", "b@y.com"],
    )
    assert "ORGANIZER;CN=me@example.com:mailto:me@example.com" in body
    assert "ATTENDEE" in body
    assert "mailto:a@x.com" in body
    assert "mailto:b@y.com" in body


def test_invalid_date_raises():
    import pytest
    with pytest.raises(ValueError, match="unrecognized"):
        ics.build_ics(subject="x", start="not a date", end="2026-07-25 10:00")
