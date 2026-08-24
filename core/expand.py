"""Recurrence expansion: rules in, rows out.

Why materialise at all: the thing this program is for is having *many*
calendars open at once. Drawing a fortnight then means asking twenty
collections what they contain, and if the answer involves running every
recurrence rule in each of them per repaint, the view is slow in exactly the
case it exists for. Expanded rows turn it back into one range scan over one
index, whatever the number of calendars.

What that costs is a horizon: an infinite series exists as rows only between
``horizon_past_days`` and ``horizon_future_days`` around today. The window rolls
forward (``roll_horizon``), and anything outside it is still in ``events``: it
is the drawing that is bounded, not the data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Iterator

from dateutil.rrule import rrulestr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Event, Occurrence
from .timeutil import days_touched, from_utc, to_utc, utcnow, zone

# A rule that produces more instances than this in one horizon is a mistake or
# an attack, not a calendar: a DAILY rule over the default horizon is under two
# thousand, and a MINUTELY one is millions. Stopping is better than filling the
# table. The event is still there, and only its tail is missing.
MAX_INSTANCES = 5000


def recurrence_key(wall: datetime, all_day: bool) -> str:
    """How a RECURRENCE-ID is written for matching.

    Only ever compared against another key made here, so the exact spelling
    does not matter. What matters is that an override and the instance it
    replaces produce the same string, which is why it is built from the *local*
    wall time both of them are stated in.
    """
    return wall.strftime("%Y%m%d") if all_day else wall.strftime("%Y%m%dT%H%M%S")


def _parse_dates(raw: str) -> list[datetime]:
    """EXDATE/RDATE as stored: comma-separated local wall times, ISO."""
    out: list[datetime] = []
    for part in (raw or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(datetime.fromisoformat(part))
        except ValueError:
            continue  # one unparsable date must not cost the whole series
    return out


def instances(
    event: Event,
    window_start: datetime,
    window_end: datetime,
    skip: Iterable[str] = (),
) -> Iterator[tuple[datetime, datetime]]:
    """Every appearance of ``event`` that overlaps [window_start, window_end),
    as naive-UTC (start, end) pairs.

    An occurrence *overlaps* the window if it has not ended before the window
    starts, which is the whole reason the search below begins a duration
    earlier than the window does. A three-week event that began last month is
    the most important thing on today's screen, and a naive "starts within the
    range" query is exactly what loses it.
    """
    skip_keys = set(skip)
    duration = timedelta(seconds=max(event.duration_s, 0))
    tz = zone(event.tz_id)

    if not event.rrule and not event.rdate:
        start, end = event.dtstart, event.dtend
        if end > window_start and start < window_end:
            yield start, end
        return

    # The rule is stated in wall time, so the window has to be too, widened by
    # the event's own length, plus a day either side to cover the zone offset.
    if event.all_day:
        # An all-day series is stated in dates; the window's UTC bounds are the
        # same numbers, and converting them would be the bug this branch exists
        # to avoid.
        lo, hi = window_start - duration - timedelta(days=1), window_end + timedelta(days=1)
    else:
        lo = from_utc(window_start, tz) - duration - timedelta(days=1)
        hi = from_utc(window_end, tz) + timedelta(days=1)

    rule_text = "\n".join(
        line for line in (
            *(f"RRULE:{r}" for r in event.rrule.splitlines() if r.strip()),
            *(f"EXDATE:{d.strftime('%Y%m%dT%H%M%S')}" for d in _parse_dates(event.exdate)),
        )
    )
    try:
        rule = rrulestr(rule_text, dtstart=event.dtstart_local, forceset=True)
    except (ValueError, TypeError):
        # A rule this host cannot read still has a first instance, and showing
        # that is better than dropping the event out of the calendar.
        start, end = event.dtstart, event.dtend
        if end > window_start and start < window_end:
            yield start, end
        return

    for extra in _parse_dates(event.rdate):
        rule.rdate(extra)

    count = 0
    for wall in rule.between(lo, hi, inc=True):
        if recurrence_key(wall, event.all_day) in skip_keys:
            continue
        start = wall if event.all_day else to_utc(wall, event.tz_id)
        end = start + duration
        if end <= window_start or start >= window_end:
            continue
        yield start, end
        count += 1
        if count >= MAX_INSTANCES:
            return


def horizon(settings, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or utcnow()
    return (
        now - timedelta(days=settings.horizon_past_days),
        now + timedelta(days=settings.horizon_future_days),
    )


def _override_keys(db: Session, event: Event) -> set[str]:
    """The instances of this series that some other row replaces.

    Only asked for masters. An override is a sibling row (same calendar, same
    UID, a RECURRENCE-ID) and its own single occurrence is written when *it*
    is rebuilt, so the master must leave that slot empty or the day shows the
    meeting twice: once where it was, once where it was moved to.
    """
    if event.recurrence_id or not (event.rrule or event.rdate):
        return set()
    rows = db.execute(
        select(Event.recurrence_id).where(
            Event.calendar_id == event.calendar_id,
            Event.uid == event.uid,
            Event.recurrence_id != "",
        )
    ).scalars()
    return {r for r in rows if r}


def rebuild_event(db: Session, event: Event, window: tuple[datetime, datetime]) -> int:
    """Replace this event's occurrence rows for the window. Returns how many.

    Delete-then-insert rather than a diff: a rule change can move every
    instance, the rows carry nothing the event does not, and the whole set for
    one event is small. Correctness for free is worth more than the writes.
    """
    db.execute(delete(Occurrence).where(Occurrence.event_id == event.id))
    skip = _override_keys(db, event)
    rows = []
    for start, end in instances(event, window[0], window[1], skip):
        rows.append(
            Occurrence(
                event_id=event.id,
                calendar_id=event.calendar_id,
                start_utc=start,
                end_utc=end,
                all_day=event.all_day,
                span_days=days_touched(start, end),
            )
        )
    db.add_all(rows)
    return len(rows)


def rebuild_series(db: Session, event: Event, window: tuple[datetime, datetime]) -> int:
    """Rebuild an event *and* the series it belongs to.

    Writing an override changes which instances the master may draw, so the
    master has to be rebuilt with it. This is the entry point the agent uses,
    because it is the one that cannot leave a duplicate behind.
    """
    total = rebuild_event(db, event, window)
    if event.recurrence_id:
        master = db.execute(
            select(Event).where(
                Event.calendar_id == event.calendar_id,
                Event.uid == event.uid,
                Event.recurrence_id == "",
            )
        ).scalar_one_or_none()
        if master is not None:
            total += rebuild_event(db, master, window)
    return total


def rebuild_calendar(db: Session, calendar_id: int, window: tuple[datetime, datetime]) -> int:
    events = db.execute(select(Event).where(Event.calendar_id == calendar_id)).scalars().all()
    return sum(rebuild_event(db, e, window) for e in events)


def roll_horizon(db: Session, settings, force: bool = False) -> int:
    """Re-expand everything when the window has moved on.

    The horizon is relative to *now*, so a process that has been up for a month
    is drawing a month less future than it promises, and a weekly meeting's
    rows simply stop at the edge, silently, which is the worst way for a
    calendar to be wrong. Rebuilding is cheap enough to do daily: it is one
    pass over ``events``, which is thousands of rows, not millions.

    Returns the number of occurrences written, or -1 when there was nothing to
    do.
    """
    from .models import Setting  # local: models imports this module's siblings

    now = utcnow()
    row = db.get(Setting, "horizon")
    last = (row.value or {}).get("rolled_at") if row else None
    if not force and last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 24 * 3600:
                return -1
        except ValueError:
            pass

    window = horizon(settings, now)
    total = 0
    for event in db.execute(select(Event)).scalars().all():
        total += rebuild_event(db, event, window)
    value = {"rolled_at": now.isoformat(), "start": window[0].isoformat(), "end": window[1].isoformat()}
    if row is None:
        db.add(Setting(key="horizon", value=value))
    else:
        row.value = value
    db.commit()
    return total
