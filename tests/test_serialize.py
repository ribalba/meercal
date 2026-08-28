"""Which events are drawn with somebody else on them.

The question the views ask is "am I meeting anyone here", and the honest answer
depends on knowing which addresses are the reader's own -- knowledge the browser
does not have, which is why the decision is made here and shipped as a flag.

The cases below are the ones a real calendar actually produces: a meeting, an
invitation whose guest list the server would not disclose, a solo entry, and the
birthday Google manufactures out of your address book and hands back looking
exactly like a meeting you are the only guest at.
"""

from __future__ import annotations

from datetime import datetime

from app.serialize import NO_ORGANIZER, guest_addresses, with_people
from core.models import Event

MINE = frozenset({"ribalba@gmail.com", "apple@ribalba.de"})


def an_event(**kw) -> Event:
    event = Event(
        calendar_id=1, uid="abc@example.com", recurrence_id="",
        summary="Jour fixe", location="", description="",
        dtstart=datetime(2026, 8, 24, 7, 30), dtend=datetime(2026, 8, 24, 8, 30),
        dtstart_local=datetime(2026, 8, 24, 9, 30), tz_id="Europe/Berlin",
        duration_s=3600, all_day=False, rrule="", sequence=1,
        organizer="", attendees=[], transparent=False, status="",
    )
    for key, value in kw.items():
        setattr(event, key, value)
    return event


def guest(email: str) -> dict:
    return {"email": email, "name": email.split("@")[0], "status": "ACCEPTED", "role": "REQ-PARTICIPANT"}


def test_a_note_to_yourself_is_not_a_meeting():
    assert with_people(an_event(), MINE) is False


def test_a_guest_who_is_not_you_is_a_meeting():
    event = an_event(organizer="ribalba@gmail.com",
                     attendees=[guest("ribalba@gmail.com"), guest("anna@example.com")])
    assert with_people(event, MINE) is True
    assert guest_addresses(event, MINE) == ["anna@example.com"]


def test_an_invitation_counts_even_with_the_guest_list_stripped():
    """Free/busy calendars hand over the organiser and nothing else."""
    event = an_event(organizer="noreply-indico-team@cern.ch", attendees=[])
    assert with_people(event, MINE) is True
    assert guest_addresses(event, MINE) == []


def test_a_guest_list_of_only_you_is_not_a_meeting():
    """Google's generated birthdays, and there are forty-odd of them a year."""
    event = an_event(summary="Tony Hey's birthday", all_day=True,
                     organizer=NO_ORGANIZER, attendees=[guest("ribalba@gmail.com")])
    assert with_people(event, MINE) is False
    assert guest_addresses(event, MINE) == []


def test_your_own_organiser_line_is_not_somebody_else():
    assert with_people(an_event(organizer="APPLE@ribalba.de"), MINE) is False


def test_addresses_are_matched_without_case_or_padding():
    event = an_event(attendees=[guest(" RIBALBA@Gmail.com ")])
    assert with_people(event, MINE) is False


def test_a_guest_with_no_address_is_not_a_guest():
    """CN with no mailto: is a name on a list, not somebody to count."""
    event = an_event(attendees=[{"name": "Room 2.09", "email": "", "status": ""}])
    assert with_people(event, MINE) is False


def test_with_no_accounts_known_everything_shared_still_reads_as_shared():
    """`mine` is empty on a fresh install; the mark errs towards showing."""
    event = an_event(attendees=[guest("anna@example.com")])
    assert with_people(event, frozenset()) is True
