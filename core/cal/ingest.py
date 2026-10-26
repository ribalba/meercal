"""Writing parsed events into the database.

The only writer in normal operation is the agent, but the server uses the same
functions for the calendars it owns itself (a local calendar, an imported .ics)
so that there is one definition of what storing an event means, including
keeping ``occurrences`` in step, which is the part that is easy to forget and
invisible when it goes wrong.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session, aliased

from ..expand import rebuild_series
from ..models import (
    CALENDAR_COLORS,
    Account,
    Calendar,
    CalendarSetMember,
    Event,
    EventReminder,
    PendingAction,
)
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
    # `color_pinned` is that choice made explicitly, in the sidebar; without it
    # picking another palette colour would last exactly until the next pass.
    if color and not cal.color_pinned and not cal.display_name and cal.color in CALENDAR_COLORS:
        cal.color = color
    return cal


def _collection(url: str) -> str:
    return urlsplit(url).path.rstrip("/")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def rehome_calendar(db: Session, account: Account, url: str) -> None:
    """Make the calendar at ``url`` the only one this account holds for it.

    iCloud serves one account from more than one host. Discovery has answered
    ``p106-caldav.icloud.com`` on one pass and ``p190-caldav.icloud.com`` on
    the next, for the same principal, with the same paths, etags and sync
    tokens behind both. Keyed by the whole URL, every calendar then existed
    twice. Each copy was synced on every other pass and every event was drawn
    twice.

    So a CalDAV collection is its path within the account. A calendar held
    under another host is moved to this one, with its events, and the exact
    URL comparisons of the sync pass keep working. Copies an earlier pass made
    are folded into the oldest calendar, which is the one the user's colours,
    sets and reminder choices were made on. The events of a copy are copies
    of the server's, so dropping them loses nothing.

    A copy with writes still queued against it is left for a later pass. The
    edit lives in the copy's own row until the queue has carried it to the
    server, and folding the copy now would throw the edit away.
    """
    held = [
        cal
        for cal in db.execute(
            select(Calendar).where(Calendar.account_id == account.id).order_by(Calendar.id)
        ).scalars()
        if _collection(cal.url) == _collection(url)
    ]
    if not held or [cal.url for cal in held] == [url]:
        return
    keep, copies = held[0], held[1:]
    if copies and db.execute(
        select(PendingAction.id).where(
            PendingAction.calendar_id.in_([cal.id for cal in copies]),
            PendingAction.state == "queued",
        ).limit(1)
    ).first():
        return
    for copy in copies:
        _fold(db, copy, keep)
    # The copy may hold `url` itself, and the unique constraint wants it gone
    # before `keep` takes the URL over.
    db.flush()
    _rehost(db, keep, url)
    db.flush()


def _fold(db: Session, copy: Calendar, keep: Calendar) -> None:
    """Move what the user made on ``copy`` over to ``keep`` and drop the rest.

    Reminder choices and set memberships are moved unless ``keep`` already has
    its own. The queue's history is moved so that a failed write stays in
    view. Reminder deliveries go with the copy, because ``keep`` armed its own
    for the same instances.
    """
    mine = aliased(EventReminder)
    db.execute(
        update(EventReminder)
        .where(
            EventReminder.calendar_id == copy.id,
            ~exists().where(
                mine.calendar_id == keep.id,
                mine.uid == EventReminder.uid,
                mine.recurrence_id == EventReminder.recurrence_id,
            ),
        )
        .values(calendar_id=keep.id)
    )
    member = aliased(CalendarSetMember)
    db.execute(
        update(CalendarSetMember)
        .where(
            CalendarSetMember.calendar_id == copy.id,
            ~exists().where(member.calendar_id == keep.id, member.set_id == CalendarSetMember.set_id),
        )
        .values(calendar_id=keep.id)
    )
    db.execute(
        update(PendingAction).where(PendingAction.calendar_id == copy.id).values(calendar_id=keep.id)
    )
    db.delete(copy)


def _rehost(db: Session, cal: Calendar, url: str) -> None:
    """Point ``cal`` and everything under it at the host ``url`` names."""
    old, new = _origin(cal.url), _origin(url)
    if old != new:
        db.execute(
            update(Event)
            .where(Event.calendar_id == cal.id, Event.url.startswith(old + "/", autoescape=True))
            .values(url=func.concat(new, func.substr(Event.url, len(old) + 1)))
        )
        # A queued delete carries the URL it is to delete in its payload.
        for action in db.execute(
            select(PendingAction).where(
                PendingAction.calendar_id == cal.id, PendingAction.state == "queued"
            )
        ).scalars():
            href = action.payload.get("url") or ""
            if href.startswith(old + "/"):
                action.payload = {**action.payload, "url": new + href[len(old):]}
    cal.url = url


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
        "rrule", "rdate", "exdate", "organizer", "attendees", "categories", "alarms",
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

    Replaces the lot. A resource that loses an override (the moved instance
    put back where it belongs) has to lose the row too, and a diff by UID
    cannot see that: the deletion is the absence of a component, not a
    component saying it was deleted.

    Except for a row with no URL. That one was never on the server: it was made
    here (a moved instance imported from an invitation) and is waiting in the
    queue to be written into this very resource. The agent syncs before it
    drains, so a series that changed on the server in the meantime would
    otherwise delete the override before it was ever sent, and the queued write
    would find nothing left to write.
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
        if (event.uid, event.recurrence_id) not in keep and event.url:
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

    Only safe after a pass that listed the *whole* collection: an incremental
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
