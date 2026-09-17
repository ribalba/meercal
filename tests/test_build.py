from datetime import datetime

import pytest

from core.cal import build as build_mod

from core.cal.build import add_exdate, event_to_ics, has_vevent, new_uid, patch_ics, splice_ics
from core.cal.parse import parse_calendar
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


# --- one VEVENT inside a whole resource ----------------------------------------
#
# A series and the instances moved out of it are one resource on the server, and
# a PUT replaces all of it. Writing one row as if it were the resource is how an
# edit to the series deleted every moved instance, and a delete of one moved
# instance deleted the series.

UID = "standup@example.com"
MOVED_KEY = "20260914T093000"

HEAD = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Apple Inc.//iOS 18//EN"]

BERLIN = [
    "BEGIN:VTIMEZONE", "TZID:Europe/Berlin",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST",
    "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET",
    "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE",
]

# What Exchange writes: its own name for the zone, and a VTIMEZONE to say what
# that name means.
EXCHANGE_ZONE = [
    "BEGIN:VTIMEZONE", "TZID:W. Europe Standard Time",
    "BEGIN:STANDARD", "DTSTART:16010101T030000", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100",
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10", "END:STANDARD",
    "BEGIN:DAYLIGHT", "DTSTART:16010101T020000", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200",
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3", "END:DAYLIGHT",
    "END:VTIMEZONE",
]

MASTER = [
    "BEGIN:VEVENT", f"UID:{UID}", "DTSTAMP:20260801T090000Z",
    "DTSTART;TZID=Europe/Berlin:20260907T093000",
    "DTEND;TZID=Europe/Berlin:20260907T094500",
    "RRULE:FREQ=WEEKLY;BYDAY=MO",
    "SUMMARY:Standup",
    "DESCRIPTION:The roadmap, the hiring plan, and whatever else comes up on th",
    " e day itself.",
    "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
    "BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "END:VALARM",
    "END:VEVENT",
]

MOVED = [
    "BEGIN:VEVENT", f"UID:{UID}", "DTSTAMP:20260801T090000Z",
    f"RECURRENCE-ID;TZID=Europe/Berlin:{MOVED_KEY}",
    "DTSTART;TZID=Europe/Berlin:20260914T133000",
    "DTEND;TZID=Europe/Berlin:20260914T134500",
    "SUMMARY:Standup (moved)",
    "END:VEVENT",
]


def calendar(*parts: list[str], newline: str = "\r\n") -> str:
    return newline.join([*HEAD, *(ln for part in parts for ln in part), "END:VCALENDAR"]) + newline


def exchange(lines: list[str]) -> list[str]:
    return [ln.replace("TZID=Europe/Berlin", "TZID=W. Europe Standard Time") for ln in lines]


def an_override(**kw) -> Event:
    """The 14 September standup, moved again: 15:00 in Berlin this time."""
    fields = dict(
        uid=UID, recurrence_id=MOVED_KEY, summary="Standup (moved again)",
        dtstart=datetime(2026, 9, 14, 13), dtend=datetime(2026, 9, 14, 13, 15),
        dtstart_local=datetime(2026, 9, 14, 15), duration_s=900, sequence=2,
    )
    return an_event(**{**fields, **kw})


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_a_moved_instance_is_replaced_inside_its_series_and_nothing_else_changes(newline):
    """The master, its folded description, its alarm, the zone: all of it goes
    back to the server as the server wrote it. Only the line endings are ours."""
    series = calendar(BERLIN, MASTER, MOVED, newline=newline)
    out = splice_ics(series, UID, MOVED_KEY, event_to_ics(an_override()))

    assert out.startswith("\r\n".join([*HEAD, *BERLIN, *MASTER]) + "\r\n")
    assert out.endswith("END:VEVENT\r\nEND:VCALENDAR\r\n")
    assert "Standup (moved)\r\n" not in out
    events = parse_calendar(out)
    assert [(e.recurrence_id, e.summary) for e in events] == [
        ("", "Standup"), (MOVED_KEY, "Standup (moved again)"),
    ]
    assert events[0].alarms and events[0].description.endswith("day itself.")


def test_an_instance_the_series_does_not_hold_yet_is_added_to_it():
    only_master = calendar(BERLIN, MASTER)
    out = splice_ics(only_master, UID, MOVED_KEY, event_to_ics(an_override()))

    assert out.startswith(only_master.removesuffix("END:VCALENDAR\r\n"))
    assert out.endswith("END:VEVENT\r\nEND:VCALENDAR\r\n")
    assert [(e.recurrence_id, e.summary) for e in parse_calendar(out)] == [
        ("", "Standup"), (MOVED_KEY, "Standup (moved again)"),
    ]


def test_an_exchange_zone_name_is_the_same_instance_as_its_iana_name():
    """Outlook writes `TZID=W. Europe Standard Time`, meercal writes
    `TZID=Europe/Berlin`, and the 09:30 instance is one instance either way.
    Matched as text, the splice would add a second copy beside the first and
    the day would show the meeting twice.

    Relies on core.cal.parse reading a Windows zone name as its IANA zone
    (core.timeutil.zone_name): the key is worked out there, not here."""
    series = calendar(EXCHANGE_ZONE, exchange(MASTER), exchange(MOVED))
    out = splice_ics(series, UID, MOVED_KEY, event_to_ics(an_override()))

    assert out.count("BEGIN:VEVENT") == 2
    assert "Standup (moved)\r\n" not in out
    assert "\r\n".join(EXCHANGE_ZONE) in out
    assert [(e.recurrence_id, e.summary) for e in parse_calendar(out)] == [
        ("", "Standup"), (MOVED_KEY, "Standup (moved again)"),
    ]


def test_taking_one_vevent_out_leaves_the_rest_exactly_as_it_was():
    series = calendar(BERLIN, MASTER, MOVED)
    assert splice_ics(series, UID, MOVED_KEY, None) == calendar(BERLIN, MASTER)
    assert splice_ics(series, UID, "", None) == calendar(BERLIN, MOVED)
    # Nothing matching is nothing removed.
    assert splice_ics(series, UID, "20260921T093000", None) == series
    assert not has_vevent(splice_ics(calendar(BERLIN, MOVED), UID, MOVED_KEY, None))


def test_a_body_that_is_not_a_calendar_is_never_written_over():
    """A proxy's error page answered with 200 is still not a series, and a PUT
    of one VEVENT in its place would be the whole series gone."""
    with pytest.raises(ValueError):
        splice_ics("<html>Service unavailable</html>", UID, MOVED_KEY, event_to_ics(an_override()))
    # Nothing at all is a resource that does not exist yet: the VEVENT, wrapped.
    fresh = splice_ics("", UID, MOVED_KEY, event_to_ics(an_override()))
    assert fresh.startswith("BEGIN:VCALENDAR\r\n") and fresh.endswith("END:VCALENDAR\r\n")
    assert [e.recurrence_id for e in parse_calendar(fresh)] == [MOVED_KEY]


@pytest.mark.parametrize(
    "start, end, key, exdate",
    [
        ("DTSTART;TZID=Europe/Berlin:20260907T093000", "DTEND;TZID=Europe/Berlin:20260907T094500",
         MOVED_KEY, f"EXDATE;TZID=Europe/Berlin:{MOVED_KEY}"),
        ("DTSTART;VALUE=DATE:20260907", "DTEND;VALUE=DATE:20260908",
         "20260914", "EXDATE;VALUE=DATE:20260914"),
        ("DTSTART:20260907T073000Z", "DTEND:20260907T074500Z",
         "20260914T073000", "EXDATE:20260914T073000Z"),
        ("DTSTART:20260907T093000", "DTEND:20260907T094500",
         MOVED_KEY, f"EXDATE:{MOVED_KEY}"),
    ],
    ids=["zoned", "all-day", "utc", "floating"],
)
def test_an_exdate_is_written_the_way_the_master_writes_its_start(start, end, key, exdate):
    master = [start if ln.startswith("DTSTART") else end if ln.startswith("DTEND") else ln
              for ln in MASTER]
    out = add_exdate(calendar(BERLIN, master), UID, key)

    assert f"\r\n{exdate}\r\n" in out
    # A component's properties come before its alarms.
    assert out.index(exdate) < out.index("BEGIN:VALARM")
    (series,) = parse_calendar(out)
    assert series.exdate


def test_an_exdate_for_an_exchange_series_keeps_the_exchange_zone_name():
    out = add_exdate(calendar(EXCHANGE_ZONE, exchange(MASTER)), UID, MOVED_KEY)
    assert f"\r\nEXDATE;TZID=W. Europe Standard Time:{MOVED_KEY}\r\n" in out


def test_an_instance_is_excluded_once_and_only_from_a_master():
    once = add_exdate(calendar(BERLIN, MASTER), UID, MOVED_KEY)
    assert add_exdate(once, UID, MOVED_KEY).count("EXDATE") == 1
    # A resource holding only a moved instance has no series to exclude it from.
    assert "EXDATE" not in add_exdate(calendar(BERLIN, MOVED), UID, MOVED_KEY)


def test_an_override_is_written_with_its_recurrence_id_in_its_own_zone():
    assert f"RECURRENCE-ID;TZID=Europe/Berlin:{MOVED_KEY}" in event_to_ics(an_override())
    all_day = an_override(recurrence_id="20260914", all_day=True,
                          dtstart=datetime(2026, 9, 15), duration_s=86400)
    assert "RECURRENCE-ID;VALUE=DATE:20260914" in event_to_ics(all_day)
    assert "RECURRENCE-ID" not in event_to_ics(an_event())


def test_patching_an_override_restates_its_recurrence_id_beside_its_start():
    """An Exchange invitation's zone name goes out as the IANA name on DTSTART,
    so it has to on RECURRENCE-ID as well, or the one VEVENT names two zones."""
    bare = "\r\n".join(exchange(MOVED)) + "\r\n"
    patched = patch_ics(bare, an_override())

    assert patched.count("RECURRENCE-ID") == 1
    assert f"RECURRENCE-ID;TZID=Europe/Berlin:{MOVED_KEY}" in patched
    assert "DTSTART;TZID=Europe/Berlin:20260914T150000" in patched
    # A master has no RECURRENCE-ID, and patching one does not give it one.
    assert "RECURRENCE-ID" not in patch_ics(ORIGINAL, an_event())


def test_an_override_edited_in_another_zone_still_names_the_instance_it_replaces():
    """The key is a wall time in the zone the override arrived in. The panel
    restates an edited row in the display zone and leaves the key alone, so the
    RECURRENCE-ID has to carry the instant across rather than the numbers: 09:30
    in New York is 15:30 in Berlin, and 09:30 in Berlin is no instance at all."""
    new_york = "\r\n".join(ln.replace("TZID=Europe/Berlin", "TZID=America/New_York")
                           for ln in MOVED) + "\r\n"
    edited = an_override(raw_ics=new_york)          # tz_id is Europe/Berlin now
    patched = patch_ics(new_york, edited)

    assert "RECURRENCE-ID;TZID=Europe/Berlin:20260914T153000" in patched
    assert patched.count("RECURRENCE-ID") == 1
