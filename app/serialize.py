"""Rows to JSON.

One rule shapes this file: **the server does the timezone arithmetic, once.**
Occurrences leave here as wall-clock strings in the display zone with no offset
on them, and the browser never converts anything. A calendar drawn from
instants and a browser that disagrees with the server about the zone is a class
of bug that only shows up twice a year, in the week nobody is looking, and this
is what makes it impossible rather than rare.

All-day events are the other half of the rule: they are dates, they are sent as
dates, and nothing on either side moves them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.models import Account, Calendar, Event, Occurrence
from core.timeutil import days_touched, display_zone, from_utc

settings = get_settings()
TZ = display_zone(settings.timezone)

# How much of the description travels with a range query. The full text is one
# request away (GET /api/events/{id}); a fortnight of twenty calendars is not
# the place to ship every meeting agenda in full.
DESCRIPTION_PREVIEW = 280

# Google writes this as the ORGANIZER of the birthday and holiday entries it
# generates for you. It is a literal statement that the event has no organiser,
# and taking it at face value is worth doing: those entries are also given a
# guest list of one, and that one is you.
NO_ORGANIZER = "unknownorganizer@calendar.google.com"


def own_addresses(db: Session) -> frozenset[str]:
    """Every address that is you, over all accounts.

    The union rather than per-account: an address is the same person whichever
    server it is being read through, and the alternative -- joining every
    occurrence back to the account that owns its calendar -- buys nothing but
    the join. Accounts are counted in single figures, so this is one small
    query per request.
    """
    rows = db.execute(select(Account.username)).scalars().all()
    return frozenset(u.strip().lower() for u in rows if u and "@" in u)


def with_people(ev: Event, mine: frozenset[str]) -> bool:
    """Is anyone *else* on this event?

    Two things say so, and either is enough:

    * a guest who is not you; or
    * an organiser who is neither you nor Google's "no organiser" above. An
      invitation frequently arrives with the rest of the guest list stripped --
      a calendar you hold free/busy on, a server that will not disclose the
      room -- and it is still an invitation.

    The "not you" is what earns its keep. A guest list whose only name is your
    own is not a meeting: it is how several servers mark an entry as belonging
    to you, and every birthday Google syncs out of your contacts has exactly
    that shape. Counting those would put a person against forty-odd days a year
    that nobody is meeting anybody on, which is the quickest way to teach a
    reader to stop seeing the mark at all.
    """
    organizer = (ev.organizer or "").strip().lower()
    if organizer and organizer != NO_ORGANIZER and organizer not in mine:
        return True
    return any(guest_addresses(ev, mine))


def guest_addresses(ev: Event, mine: frozenset[str]) -> list[str]:
    """The guest list with you taken out of it, lowercased."""
    out = []
    for person in ev.attendees or ():
        email = str((person or {}).get("email") or "").strip().lower()
        if email and email not in mine:
            out.append(email)
    return out


def wall(instant, all_day: bool) -> str:
    """A stored instant as the wall time the view draws it at."""
    value = instant if all_day else from_utc(instant, TZ)
    return value.isoformat(timespec="seconds")


def occurrence_json(
    occ: Occurrence, calendar: Calendar | None = None, mine: frozenset[str] = frozenset()
) -> dict:
    ev = occ.event
    start = occ.start_utc if occ.all_day else from_utc(occ.start_utc, TZ)
    end = occ.end_utc if occ.all_day else from_utc(occ.end_utc, TZ)
    return {
        "id": occ.id,
        "event_id": ev.id,
        "uid": ev.uid,
        "cal": occ.calendar_id,
        "title": ev.summary or "(no title)",
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "all_day": occ.all_day,
        # Recomputed in the display zone rather than read from the row: the
        # stored value counts UTC days, which is the right thing for the index
        # and the wrong thing for a bar drawn over dates in Berlin.
        "span_days": days_touched(start, end),
        "location": ev.location,
        "preview": ev.description[:DESCRIPTION_PREVIEW],
        "has_more": len(ev.description) > DESCRIPTION_PREVIEW,
        "status": ev.status,
        "free": ev.transparent,
        "recurring": bool(ev.rrule or ev.rdate),
        "override": bool(ev.recurrence_id),
        "organizer": ev.organizer,
        "attendees": ev.attendees[:12],
        "attendee_count": len(ev.attendees),
        # Whether to draw the person against this event, decided here: the
        # browser has no idea which addresses are the reader's own, and the
        # answer is the same for every view that asks.
        "with_people": with_people(ev, mine),
        "guest_count": len(guest_addresses(ev, mine)),
        "read_only": bool(calendar.read_only) if calendar else False,
    }


def calendar_json(cal: Calendar) -> dict:
    return {
        "id": cal.id,
        "account_id": cal.account_id,
        "name": cal.label,
        "server_name": cal.name,
        "color": cal.color,
        "visible": cal.visible,
        "read_only": cal.read_only,
        "position": cal.position,
        "tz": cal.tz_id,
        "last_sync_at": cal.last_sync_at.isoformat() if cal.last_sync_at else None,
        "error": cal.last_error,
    }
