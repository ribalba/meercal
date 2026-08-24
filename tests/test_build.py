from datetime import datetime

from core.cal.build import event_to_ics, new_uid, patch_ics
from core.models import Event


def an_event(**kw) -> Event:
    event = Event(
        calendar_id=1, uid="abc@example.com", recurrence_id="",
        summary="Jour fixe", location="Room 2", description="",
        dtstart=datetime(2026, 8, 24, 7, 30), dtend=datetime(2026, 8, 24, 8, 30),
        dtstart_local=datetime(2026, 8, 24, 9, 30), tz_id="Europe/Berlin",
        duration_s=3600, all_day=False, rrule="", sequence=1,
        attendees=[], transparent=False, status="",
    )
    for key, value in kw.items():
        setattr(event, key, value)
    return event


def test_uids_are_unique_and_carry_the_app():
    assert new_uid() != new_uid()
    assert new_uid().endswith("@meercal")


def test_a_timed_event_is_written_in_its_own_zone():
    ics = event_to_ics(an_event())
    assert "DTSTART;TZID=Europe/Berlin:20260824T093000" in ics
    assert "SUMMARY:Jour fixe" in ics


def test_an_all_day_event_is_written_as_dates():
    ics = event_to_ics(an_event(all_day=True, dtstart=datetime(2026, 9, 2),
                                duration_s=19 * 86400))
    assert "DTSTART;VALUE=DATE:20260902" in ics
    assert "DTEND;VALUE=DATE:20260921" in ics


ORIGINAL = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Apple Inc.//iOS 18//EN",
    "BEGIN:VEVENT", "UID:abc@example.com",
    "DTSTART;TZID=Europe/Berlin:20260824T093000",
    "DTEND;TZID=Europe/Berlin:20260824T100000",
    "SUMMARY:Jour fixe",
    "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
    "BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR", "",
])


def test_patching_keeps_what_this_program_does_not_model():
    # The alarm and the X- property are the point: a round trip through a parse
    # and a re-serialise would drop both, and the phone that set them would
    # quietly lose its reminder.
    patched = patch_ics(ORIGINAL, an_event(summary="Jour fixe — platform"))
    assert "SUMMARY:Jour fixe — platform" in patched
    assert "SUMMARY:Jour fixe\r\n" not in patched
    assert "BEGIN:VALARM" in patched
    assert "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC" in patched
    assert patched.count("DTSTART") == 1


def test_patching_adds_a_property_the_original_never_had():
    without = ORIGINAL.replace("SUMMARY:Jour fixe\r\n", "")
    patched = patch_ics(without, an_event(location="Kreuzberg"))
    assert "LOCATION:Kreuzberg" in patched
    assert patched.index("LOCATION:Kreuzberg") < patched.index("END:VEVENT")


def test_no_original_means_building_from_scratch():
    assert "BEGIN:VCALENDAR" in patch_ics("", an_event())
