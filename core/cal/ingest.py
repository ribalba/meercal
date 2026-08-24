"""Writing parsed events into the database.

The only writer in normal operation is the agent, but the server uses the same
functions for the calendars it owns itself (a local calendar, an imported .ics)
so that there is one definition of what storing an event means — including
keeping ``occurrences`` in step, which is the part that is easy to forget and
invisible when it goes wrong.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..expand import rebuild_series
from ..models import CALENDAR_COLORS, Account, Calendar, Event
from .parse import ParsedEvent, parse_calendar


def get_or_create_account(db: Session, label: str, kind: str, url: str, username: str) -> Account:
    account = db.execute(select(Account).where(Account.label == label)).scalar_one_or_none()
    if account is None:
        account = Account(label=label, kind=kind, url=url, username=username)
        db.add(account)
        db.flush()
        return account
    # Discovery can move an account (iCloud hands out a personal host), so the
    # URL is refreshed on every pass rather than only at creation.
    account.kind, account.url, account.username = kind, url, username
    return account


def next_color(db: Session) -> str:
    """The next colour in the palette, by how many calendars already exist.

    Deliberately not random: two calendars added in the same sync pass should
    look as different as the palette can make them, and a stable order means a
    calendar keeps its colour when another one is removed.
    """
    count = db.execute(select(func.count(Calendar.id))).scalar_one() or 0
    return CALENDAR_COLORS[count % len(CALENDAR_COLORS)]


def get_or_create_calendar(
    db: Session,
    account: Account,
    url: str,
    name: str,
    *,
    color: str = "",
    tz_id: str = "",
    read_only: bool = False,
) -> Calendar:
    cal = db.execute(
        select(Calendar).where(Calendar.account_id == account.id, Calendar.url == url)
    ).scalar_one_or_none()
    if cal is None:
        cal = Calendar(
            account_id=account.id,
            url=url,
            name=name,
            color=color or next_color(db),
            tz_id=tz_id,
            read_only=read_only,
            position=db.execute(select(func.count(Calendar.id))).scalar_one() or 0,
        )
        db.add(cal)
        db.flush()
        return cal
    cal.name = name or cal.name
    if tz_id:
        cal.tz_id = tz_id
    cal.read_only = read_only
    # `color` is only ever a *suggestion* from the server: the user's choice
    # wins, and a server that reports no colour must not reset one they picked.
    if color and not cal.display_name and cal.color in CALENDAR_COLORS:
        cal.color = color
    return cal


def upsert_event(
    db: Session,
    calendar: Calendar,
    parsed: ParsedEvent,
    window: tuple[datetime, datetime],
    *,
    etag: str = "",
    url: str = "",
) -> Event:
    event = db.execute(
        select(Event).where(
            Event.calendar_id == calendar.id,
            Event.uid == parsed.uid,
            Event.recurrence_id == parsed.recurrence_id,
        )
    ).scalar_one_or_none()
    if event is None:
        event = Event(calendar_id=calendar.id, uid=parsed.uid, recurrence_id=parsed.recurrence_id)
        db.add(event)

    for attr in (
        "summary", "description", "location", "status", "transparent", "all_day",
        "dtstart", "dtend", "dtstart_local", "tz_id", "duration_s",
        "rrule", "rdate", "exdate", "organizer", "attendees", "categories",
        "sequence", "raw_ics",
    ):
        setattr(event, attr, getattr(parsed, attr))
    event.search_text = parsed.search_text
    event.etag = etag or event.etag
    event.url = url or event.url
    db.flush()

    rebuild_series(db, event, window)
    return event


def store_resource(
    db: Session,
    calendar: Calendar,
    url: str,
    ics_text: str,
    window: tuple[datetime, datetime],
    *,
    etag: str = "",
) -> int:
    """One CalDAV resource: the master and every override it carries.

    Replaces the lot. A resource that loses an override — the moved instance
    put back where it belongs — has to lose the row too, and a diff by UID
    cannot see that: the deletion is the absence of a component, not a
    component saying it was deleted.
    """
    parsed = parse_calendar(ics_text, default_tz=calendar.tz_id or "UTC")
    if not parsed:
        return 0
    keep = {(p.uid, p.recurrence_id) for p in parsed}
    stale = db.execute(
        select(Event).where(
            Event.calendar_id == calendar.id,
            Event.uid.in_({p.uid for p in parsed}),
        )
    ).scalars().all()
    for event in stale:
        if (event.uid, event.recurrence_id) not in keep:
            db.delete(event)
    db.flush()

    for p in parsed:
        upsert_event(db, calendar, p, window, etag=etag, url=url)
    return len(parsed)


def delete_resource(db: Session, calendar: Calendar, url: str) -> int:
    """Everything that came from one resource URL. Occurrences follow by cascade."""
    result = db.execute(
        delete(Event).where(Event.calendar_id == calendar.id, Event.url == url)
    )
    return result.rowcount or 0


def prune(db: Session, calendar: Calendar, seen_urls: set[str]) -> int:
    """Drop what a full pass did not find.

    Only safe after a pass that listed the *whole* collection — an incremental
    sync reports deletions itself, and using this after one would empty the
    calendar. The agent calls it only on the full-listing path.
    """
    result = db.execute(
        delete(Event).where(
            Event.calendar_id == calendar.id,
            Event.url.notin_(seen_urls or {""}),
        )
    )
    return result.rowcount or 0
