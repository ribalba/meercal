"""Reading a range, and the write path.

The read half is one query (see app/query.py) and is what every view calls.
Ribbon, week, month and day differ in how they *draw* a range, never in how
they ask for one.

The write half never speaks to a calendar server. It writes the change here and
queues a ``PendingAction``; the agent, which is the only process holding
credentials, drains that queue. The edit is on screen immediately and true a
moment later, which is the only way an edit feels instant against a server
three hundred milliseconds away.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.cal.build import new_uid
from core.config import get_settings
from core.database import get_db
from core.expand import horizon, rebuild_series
from core.models import Account, Calendar, Event, Occurrence, PendingAction
from core.timeutil import UTC, days_touched
from ..query import occurrences_in_range, parse_query
from ..serialize import TZ, occurrence_json, own_addresses
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["events"], dependencies=[Depends(require_auth)])
settings = get_settings()

# A range this long is a mistake or a script, not a view. The Ribbon asks for
# weeks at a time and pages as it scrolls; the year view asks for a year.
MAX_RANGE_DAYS = 800


def _instant(wall: datetime) -> datetime:
    """A display-zone wall time as the naive UTC the rows are stored in."""
    return wall.replace(tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)


def _parse_bound(value: str, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a date: {value!r}") from None


@router.get("/events")
def list_events(
    start: str = Query("", description="Wall-clock start, ISO, in the display zone"),
    end: str = Query(""),
    cals: str = Query("", description="Comma-separated calendar ids; empty means the visible ones"),
    q: str = Query(""),
    regex: bool = Query(False),
    hidden: bool = Query(False, description="Include calendars that are switched off"),
    db: Session = Depends(get_db),
) -> dict:
    now = datetime.now(TZ).replace(tzinfo=None)
    start_wall = _parse_bound(start, now - timedelta(days=7))
    end_wall = _parse_bound(end, start_wall + timedelta(days=42))
    if end_wall <= start_wall:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "end must be after start")
    if (end_wall - start_wall).days > MAX_RANGE_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Range longer than {MAX_RANGE_DAYS} days")

    calendar_ids = [int(c) for c in cals.split(",") if c.strip().isdigit()]
    rows = occurrences_in_range(
        db,
        _instant(start_wall),
        _instant(end_wall),
        calendar_ids=calendar_ids,
        spec=parse_query(q, regex),
        include_hidden=hidden,
    )
    calendars = {c.id: c for c in db.execute(select(Calendar)).scalars().all()}
    mine = own_addresses(db)
    return {
        "start": start_wall.isoformat(timespec="seconds"),
        "end": end_wall.isoformat(timespec="seconds"),
        "tz": str(TZ),
        "events": [occurrence_json(o, calendars.get(o.calendar_id), mine) for o in rows],
    }


@router.get("/events/{event_id}")
def get_event(event_id: int, db: Session = Depends(get_db)) -> dict:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")
    cal = db.get(Calendar, event.calendar_id)
    occ = db.execute(
        select(Occurrence).where(Occurrence.event_id == event.id).order_by(Occurrence.start_utc)
    ).scalars().first()
    payload = occurrence_json(occ, cal, own_addresses(db)) if occ else {"id": None, "event_id": event.id}
    payload |= {
        "description": event.description,
        "rrule": event.rrule,
        "attendees": event.attendees,
        "organizer": event.organizer,
        "categories": event.categories,
        "calendar": cal.label if cal else "",
        "tz": event.tz_id,
    }
    return payload


class EventBody(BaseModel):
    calendar_id: int | None = None
    title: str = ""
    start: str = ""
    end: str = ""
    all_day: bool = False
    location: str = ""
    description: str = ""
    rrule: str = ""
    attendees: list[dict] = []


def _queue(db: Session, kind: str, event: Event) -> None:
    """Tell the agent to make this true on the server it came from.

    Skipped for calendars with no server behind them: a local calendar is
    exactly a calendar whose queue would never be drained.
    """
    cal = db.get(Calendar, event.calendar_id)
    if cal is None:
        return
    account = db.get(Account, cal.account_id)
    if account is None or account.kind == "local":
        return
    db.add(
        PendingAction(
            kind=kind,
            calendar_id=cal.id,
            event_id=event.id if kind != "delete" else None,
            payload={"uid": event.uid, "url": event.url, "etag": event.etag},
        )
    )


def _apply(event: Event, body: EventBody) -> None:
    start_wall = datetime.fromisoformat(body.start)
    end_wall = datetime.fromisoformat(body.end) if body.end else start_wall + timedelta(hours=1)
    event.summary = body.title
    event.description = body.description
    event.location = body.location
    event.all_day = body.all_day
    event.rrule = body.rrule.strip()
    event.attendees = body.attendees
    if body.all_day:
        # A date, kept a date: stored midnight-to-midnight with an exclusive
        # end, and never run through a zone. See core/timeutil.
        start = datetime(start_wall.year, start_wall.month, start_wall.day)
        end = datetime(end_wall.year, end_wall.month, end_wall.day)
        if end <= start:
            end = start + timedelta(days=1)
        event.tz_id = "UTC"
        event.dtstart = event.dtstart_local = start
        event.dtend = end
    else:
        event.tz_id = str(TZ)
        event.dtstart_local = start_wall
        event.dtstart = _instant(start_wall)
        event.dtend = _instant(end_wall)
    event.duration_s = int((event.dtend - event.dtstart).total_seconds())
    event.search_text = " ".join(
        p for p in (event.summary, event.location, event.description) if p
    )


@router.post("/events", status_code=status.HTTP_201_CREATED)
def create_event(body: EventBody, db: Session = Depends(get_db)) -> dict:
    if not body.calendar_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Which calendar?")
    cal = db.get(Calendar, body.calendar_id)
    if cal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such calendar")
    if cal.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{cal.label} is read-only")
    if not body.start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An event needs a start")

    event = Event(calendar_id=cal.id, uid=new_uid(), recurrence_id="")
    _apply(event, body)
    db.add(event)
    db.flush()
    rebuild_series(db, event, horizon(settings))
    _queue(db, "create", event)
    db.commit()
    return get_event(event.id, db)


@router.patch("/events/{event_id}")
def update_event(event_id: int, body: EventBody, db: Session = Depends(get_db)) -> dict:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")
    cal = db.get(Calendar, event.calendar_id)
    if cal is not None and cal.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{cal.label} is read-only")
    # An edit to a series is an edit to the *whole* series here. Changing one
    # instance means writing an override with a RECURRENCE-ID, and doing that
    # wrong duplicates the meeting rather than moving it, so until that path
    # is written and tested, the UI says "this changes every occurrence" and
    # means it.
    _apply(event, body)
    event.sequence += 1
    db.flush()
    rebuild_series(db, event, horizon(settings))
    _queue(db, "update", event)
    db.commit()
    return get_event(event.id, db)


@router.delete("/events/{event_id}")
def delete_event(event_id: int, db: Session = Depends(get_db)) -> dict:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")
    cal = db.get(Calendar, event.calendar_id)
    if cal is not None and cal.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{cal.label} is read-only")
    _queue(db, "delete", event)
    db.delete(event)
    db.commit()
    return {"ok": True}
