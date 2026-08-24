from datetime import datetime

from core.cal.parse import parse_calendar

TIMED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:abc@example.com
DTSTART;TZID=Europe/Berlin:20260824T093000
DTEND;TZID=Europe/Berlin:20260824T100000
SUMMARY:Jour fixe
LOCATION:Room 2
RRULE:FREQ=WEEKLY;BYDAY=MO
EXDATE;TZID=Europe/Berlin:20260831T093000
ORGANIZER:mailto:you@example.com
ATTENDEE;CN=Anna Meier;PARTSTAT=ACCEPTED:mailto:anna@example.com
TRANSP:TRANSPARENT
SEQUENCE:3
END:VEVENT
END:VCALENDAR
"""

ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:trip@example.com
DTSTART;VALUE=DATE:20260902
DTEND;VALUE=DATE:20260921
SUMMARY:Tokyo
END:VEVENT
END:VCALENDAR
"""

DURATION_ONLY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:d@example.com
DTSTART:20260824T070000Z
DURATION:PT45M
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

OVERRIDE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:abc@example.com
DTSTART;TZID=Europe/Berlin:20260824T093000
DTEND;TZID=Europe/Berlin:20260824T100000
RRULE:FREQ=WEEKLY
SUMMARY:Jour fixe
END:VEVENT
BEGIN:VEVENT
UID:abc@example.com
RECURRENCE-ID;TZID=Europe/Berlin:20260831T093000
DTSTART;TZID=Europe/Berlin:20260831T140000
DTEND;TZID=Europe/Berlin:20260831T150000
SUMMARY:Jour fixe (moved)
END:VEVENT
END:VCALENDAR
"""


def test_a_timed_event_keeps_both_its_instant_and_its_wall_clock():
    (event,) = parse_calendar(TIMED)
    assert event.uid == "abc@example.com"
    assert event.tz_id == "Europe/Berlin"
    assert event.dtstart == datetime(2026, 8, 24, 7, 30)          # UTC
    assert event.dtstart_local == datetime(2026, 8, 24, 9, 30)    # what the rule means
    assert event.duration_s == 1800
    assert event.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert event.exdate == "2026-08-31T09:30:00"
    assert event.transparent is True
    assert event.sequence == 3
    assert event.organizer == "you@example.com"
    assert event.attendees == [
        {"email": "anna@example.com", "name": "Anna Meier", "status": "ACCEPTED", "role": "REQ-PARTICIPANT"}
    ]
    assert "Anna Meier" in event.search_text and "Room 2" in event.search_text


def test_an_all_day_event_is_a_date_with_an_exclusive_end():
    (event,) = parse_calendar(ALL_DAY)
    assert event.all_day is True
    assert event.dtstart == datetime(2026, 9, 2)
    assert event.dtend == datetime(2026, 9, 21)     # DTEND is the day after the last
    assert event.duration_s == 19 * 86400


def test_duration_stands_in_for_a_missing_end():
    (event,) = parse_calendar(DURATION_ONLY)
    assert event.dtend == datetime(2026, 8, 24, 7, 45)
    assert event.duration_s == 2700


def test_an_override_parses_as_its_own_event_with_a_recurrence_key():
    master, override = parse_calendar(OVERRIDE)
    assert master.recurrence_id == ""
    assert override.recurrence_id == "20260831T093000"
    assert override.dtstart_local == datetime(2026, 8, 31, 14)
    assert override.summary == "Jour fixe (moved)"


def test_rubbish_in_gives_nothing_out_rather_than_an_exception():
    assert parse_calendar("") == []
    assert parse_calendar("not a calendar at all") == []
