from datetime import datetime

import pytest

from core.cal import build as build_mod

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
    patched = patch_ics(ORIGINAL, an_event(summary="Jour fixe · platform"))
    assert "SUMMARY:Jour fixe · platform" in patched
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


FOLDED = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0",
    "BEGIN:VEVENT", "UID:abc@example.com",
    "DTSTART;TZID=Europe/Berlin:20260824T093000",
    "DTEND;TZID=Europe/Berlin:20260824T100000",
    "SUMMARY:Jour fixe",
    "DESCRIPTION:The roadmap, the hiring plan, and whatever else comes up on th",
    " e day itself.",
    "ATTENDEE;CN=Anna Meier;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED:mailto:anna@e",
    " xample.com",
    "ATTENDEE;CN=Bo Larsen;PARTSTAT=DECLINED:mailto:bo@example.com",
    "ATTENDEE;CUTYPE=RESOURCE;CN=Room 2:mailto:room-2@example.com",
    "BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR", "",
])


def guests(ics: str) -> list[str]:
    """The ATTENDEE lines, unfolded, so an assertion can be about people rather
    than about where the seventy-fifth octet happened to fall."""
    lines: list[str] = []
    for line in ics.splitlines():
        if lines and line[:1] == " ":
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return [ln for ln in lines if ln.startswith("ATTENDEE")]


def test_patching_a_folded_property_does_not_leave_the_tail_behind():
    # The continuation line of a folded DESCRIPTION is not a property, and
    # matching names against physical lines used to drop the head and glue
    # " e day itself." onto whatever replaced it.
    patched = patch_ics(FOLDED, an_event(description="Short now."))
    assert "DESCRIPTION:Short now." in patched
    assert "e day itself." not in patched


def test_a_long_line_is_folded_the_way_the_spec_folds_it():
    patched = patch_ics(FOLDED, an_event(description="x" * 200))
    for line in patched.splitlines():
        assert len(line.encode("utf-8")) <= 75


@pytest.mark.parametrize("text", ["ü" * 120, "🙂" * 60, "Grüße " * 30])
def test_folding_never_cuts_through_a_character(text):
    """75 *octets*, and an umlaut is two of them. A cut between the two halves
    of one is a description that comes back with a replacement character in it,
    or a server that rejects the whole resource as bad UTF-8."""
    line = f"DESCRIPTION:{text}"
    physical = build_mod._fold(line)
    assert all(len(p.encode("utf-8")) <= 75 for p in physical)
    assert build_mod._unfold("\r\n".join(physical)) == [line]


def test_removing_a_guest_removes_them_and_leaves_the_others_as_they_were():
    # The × in the panel, all the way to the wire. What the others carry has to
    # survive it: an acceptance, and the CUTYPE that says one of them is a room.
    kept = [
        {"email": "anna@example.com", "name": "Anna Meier",
         "status": "ACCEPTED", "role": "REQ-PARTICIPANT"},
        {"email": "room-2@example.com", "name": "Room 2",
         "status": "NEEDS-ACTION", "params": {"CUTYPE": "RESOURCE"}},
    ]
    lines = guests(patch_ics(FOLDED, an_event(attendees=kept)))
    assert len(lines) == 2
    assert not any("bo@example.com" in ln for ln in lines)
    anna = next(ln for ln in lines if "anna@example.com" in ln)
    assert "PARTSTAT=ACCEPTED" in anna and "Anna Meier" in anna
    assert "CUTYPE=RESOURCE" in next(ln for ln in lines if "room-2" in ln)


def test_removing_the_last_guest_empties_the_invitation():
    # The one property the panel owns outright. Every other patchable property
    # left empty means "unchanged", but an invitation with nobody on it is a
    # real answer, and the alarm still has nothing to do with any of it.
    patched = patch_ics(FOLDED, an_event(attendees=[]))
    assert not guests(patched)
    assert "BEGIN:VALARM" in patched


def test_a_guest_added_to_an_event_that_had_none():
    # The continuations go with the lines they belong to, or the orphaned
    # " xample.com" lands on the description and the test is about the wrong thing.
    body, drop = [], False
    for ln in FOLDED.splitlines():
        if ln[:1] == " " and drop:
            continue
        drop = ln.startswith("ATTENDEE")
        if not drop:
            body.append(ln)
    without = "\r\n".join(body)
    patched = patch_ics(without, an_event(attendees=[{"email": "cleo@example.com"}]))
    lines = guests(patched)
    assert len(lines) == 1 and "cleo@example.com" in lines[0]
    assert patched.index("ATTENDEE") < patched.index("END:VEVENT")


def test_a_patched_event_goes_back_out_as_a_whole_calendar():
    """What the database holds is the VEVENT alone -- core.cal.parse stores one
    component per row -- and a PUT of a bare component is not iCalendar. Google
    answers 400 to it, which is a save that silently never happened."""
    bare = "\r\n".join(ln for ln in FOLDED.splitlines()
                       if ln not in ("BEGIN:VCALENDAR", "VERSION:2.0", "END:VCALENDAR"))
    patched = patch_ics(bare, an_event(summary="Renamed"))
    assert patched.startswith("BEGIN:VCALENDAR\r\n")
    assert patched.rstrip().endswith("END:VCALENDAR")
    assert "VERSION:2.0" in patched
    assert patched.count("BEGIN:VEVENT") == 1
    assert "SUMMARY:Renamed" in patched


def test_a_reply_already_on_the_server_is_never_written_over():
    """The panel decides who is invited; the server decides what they said. An
    event opened before an acceptance had synced down still holds NEEDS-ACTION
    for that person, and saving an unrelated field must not put it back."""
    stale = [
        {"email": "anna@example.com", "name": "Anna Meier", "status": "NEEDS-ACTION"},
        {"email": "bo@example.com", "name": "Bo Larsen", "status": "NEEDS-ACTION"},
        {"email": "room-2@example.com", "name": "Room 2", "status": "NEEDS-ACTION"},
    ]
    lines = guests(patch_ics(FOLDED, an_event(summary="Moved", attendees=stale)))
    assert "PARTSTAT=ACCEPTED" in next(ln for ln in lines if "anna@" in ln)
    assert "PARTSTAT=DECLINED" in next(ln for ln in lines if "bo@" in ln)


def test_somebody_new_is_still_written_with_a_fresh_line():
    """The other half of it: keeping the server's line for people it knows must
    not mean a guest added here never reaches the server at all."""
    plus = [{"email": "anna@example.com", "name": "Anna Meier", "status": "NEEDS-ACTION"},
            {"email": "cleo@example.com", "name": "Cleo Ruiz", "status": "NEEDS-ACTION"}]
    lines = guests(patch_ics(FOLDED, an_event(attendees=plus)))
    assert len(lines) == 2
    assert "PARTSTAT=ACCEPTED" in next(ln for ln in lines if "anna@" in ln)
    assert "PARTSTAT=NEEDS-ACTION" in next(ln for ln in lines if "cleo@" in ln)


def test_a_quoted_parameter_containing_a_colon_still_finds_its_guest():
    """`CN="Meier: Anna"` is legal, and splitting on the first colon reads the
    address as `Anna"` -- which would file the guest under a name that matches
    nobody, and quietly write a fresh NEEDS-ACTION line over their reply."""
    odd = FOLDED.replace("ATTENDEE;CN=Bo Larsen;PARTSTAT=DECLINED:mailto:bo@example.com",
                         'ATTENDEE;CN="Larsen: Bo";PARTSTAT=DECLINED:mailto:bo@example.com')
    kept = [{"email": "bo@example.com", "name": "Bo Larsen", "status": "NEEDS-ACTION"}]
    line = guests(patch_ics(odd, an_event(attendees=kept)))[0]
    assert "PARTSTAT=DECLINED" in line and "Larsen: Bo" in line
