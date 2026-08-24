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

from core.config import get_settings
from core.models import Calendar, Occurrence
from core.timeutil import days_touched, display_zone, from_utc

settings = get_settings()
TZ = display_zone(settings.timezone)

# How much of the description travels with a range query. The full text is one
# request away (GET /api/events/{id}); a fortnight of twenty calendars is not
# the place to ship every meeting agenda in full.
DESCRIPTION_PREVIEW = 280


def wall(instant, all_day: bool) -> str:
    """A stored instant as the wall time the view draws it at."""
    value = instant if all_day else from_utc(instant, TZ)
    return value.isoformat(timespec="seconds")


def occurrence_json(occ: Occurrence, calendar: Calendar | None = None) -> dict:
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
