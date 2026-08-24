"""Time, in the three shapes this program has to keep straight.

* **Instants** — stored naive UTC, compared and indexed as such.
* **Wall times** — what a recurrence rule is written against. "Every Monday at
  09:00" survives a DST change only if it is expanded in its own zone and each
  instance converted afterwards.
* **Dates** — all-day events. Not instants at all: they are not converted
  between zones, ever, and the moment one is treated as midnight-somewhere it
  starts landing on the wrong day for somebody.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


@lru_cache(maxsize=256)
def zone(tz_id: str | None) -> ZoneInfo:
    """A zone by name, falling back to UTC rather than raising.

    A calendar server can and does send zone names this host has never heard of
    (Windows names, retired IANA aliases). One bad VTIMEZONE must not stop the
    rest of a calendar from syncing — the event lands in UTC and is off by an
    offset, which is visible and fixable; an exception here loses the calendar.
    """
    if not tz_id or tz_id.upper() == "UTC":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_id)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def system_zone_name() -> str:
    """This host's IANA zone name, by the two ways it is ever written down.

    ``datetime.now().astimezone()`` is not enough: it yields a *fixed offset*,
    which is right until the next DST change and then silently an hour out. The
    name is what carries the rules, so it is worth going after — the TZ
    environment variable if it is set, else what /etc/localtime points at.
    """
    env = os.environ.get("TZ", "").strip()
    if env:
        return env
    try:
        link = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in link:
            return link.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"


def display_zone(name: str) -> ZoneInfo:
    """The zone the UI draws in. ``"system"`` means this host's own."""
    if not name or name == "system":
        return zone(system_zone_name())
    return zone(name)


def to_utc(wall: datetime, tz_id: str) -> datetime:
    """A wall time in ``tz_id`` as a naive UTC instant.

    Ambiguous and nonexistent wall times (the hour that happens twice, and the
    one that never happens) are resolved the way every calendar server does:
    fold=0, which is the earlier of the two, and the gap shifts forward.
    """
    aware = wall.replace(tzinfo=zone(tz_id))
    return aware.astimezone(UTC).replace(tzinfo=None)


def from_utc(instant: datetime, tz: ZoneInfo) -> datetime:
    """A naive UTC instant as a naive wall time in ``tz``."""
    return instant.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def days_touched(start: datetime, end: datetime) -> int:
    """How many calendar days an interval covers, counting from its own dates.

    The subtraction is against ``end`` less one microsecond so that an event
    running to exactly midnight does not claim the day after it — a 09:00–24:00
    shift is one day, and the next day belongs to whatever starts in it.
    """
    if end <= start:
        return 1
    last = end - timedelta(microseconds=1)
    return (last.date() - start.date()).days + 1


def week_start_of(d: date, week_start: int) -> date:
    """The first day of ``d``'s week. ``week_start`` is 1 (Monday) or 7 (Sunday)."""
    offset = d.isoweekday() - week_start
    if offset < 0:
        offset += 7
    return d - timedelta(days=offset)


def iso_week_label(d: date) -> str:
    """``2026-W35``. The week number is how a technologist names a week, and it
    is the one label that stays unambiguous when the view has no month header
    in sight."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def utcnow() -> datetime:
    """Now, as the naive UTC everything is stored in.

    One definition, because ``datetime.utcnow()`` is deprecated and the
    replacement spelling is long enough that it would otherwise be written
    differently in each of the eight places that need it.
    """
    return datetime.now(UTC).replace(tzinfo=None)
