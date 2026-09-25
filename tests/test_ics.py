from datetime import datetime, timezone

import pytest

from app.ics import CalendarParseError, expand_events, parse_ics


def test_parse_and_expand_recurring_ics_events():
    feed = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:school-1\r\nDTSTART:20260925T090000Z\r\nDTEND:20260925T100000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\nEXDATE:20260926T090000Z\r\nSUMMARY:Skole\r\nLOCATION:Aula\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    events = parse_ics(feed, source_id=3)
    occurrences = expand_events(
        events,
        datetime(2026, 9, 24, tzinfo=timezone.utc),
        datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    assert len(occurrences) == 2
    assert [item["start_at"][:10] for item in occurrences] == ["2026-09-25", "2026-09-27"]
    assert occurrences[0]["location"] == "Aula"


def test_parse_all_day_ics_event():
    feed = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:holiday-1\r\nDTSTART;VALUE=DATE:20260925\r\nDTEND;VALUE=DATE:20260926\r\nSUMMARY:Helligdag\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    event = parse_ics(feed, source_id=1)[0]
    assert event["all_day"] is True
    assert event["start_at"] == "2026-09-25"
    assert event["end_at"] == "2026-09-26"


def test_recurring_school_time_keeps_wall_clock_across_dst():
    feed = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:school-dst\r\nDTSTART;TZID=Europe/Copenhagen:20261019T090000\r\nDTEND;TZID=Europe/Copenhagen:20261019T100000\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Skoletime\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    event = parse_ics(feed, source_id=1)[0]
    occurrences = expand_events(
        [event],
        datetime(2026, 10, 19, tzinfo=timezone.utc),
        datetime(2026, 11, 10, tzinfo=timezone.utc),
        "Europe/Copenhagen",
    )
    assert event["recurrence_timezone"] == "Europe/Copenhagen"
    assert [item["local_start_time"] for item in occurrences] == ["09:00"] * 4
    assert occurrences[0]["start_at"].endswith("+00:00")
    assert occurrences[0]["start_at"][11:16] == "07:00"
    assert occurrences[-1]["start_at"][11:16] == "08:00"


def test_minutely_recurrence_is_rejected():
    feed = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:too-dense\r\nDTSTART:20260925T090000Z\r\nRRULE:FREQ=MINUTELY;COUNT=1000000\r\nSUMMARY:Tat gentagelse\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    with pytest.raises(CalendarParseError, match="frequency"):
        parse_ics(feed, source_id=1)
