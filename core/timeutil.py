"""Time, in the three shapes this program has to keep straight.

* **Instants**: stored naive UTC, compared and indexed as such.
* **Wall times**: what a recurrence rule is written against. "Every Monday at
  09:00" survives a DST change only if it is expanded in its own zone and each
  instance converted afterwards.
* **Dates**: all-day events. Not instants at all: they are not converted
  between zones, ever, and the moment one is treated as midnight-somewhere it
  starts landing on the wrong day for somebody.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


@lru_cache(maxsize=256)
def zone(tz_id: str | None) -> ZoneInfo:
    """A zone by name, falling back to UTC rather than raising.

    A calendar server can and does send zone names this host has never heard of
    (Windows names, retired IANA aliases). One bad VTIMEZONE must not stop the
    rest of a calendar from syncing. The event lands in UTC and is off by an
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
    name is what carries the rules, so it is worth going after: the TZ
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
    running to exactly midnight does not claim the day after it. A 09:00–24:00
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


# --- Reminder arithmetic ---------------------------------------------------
#
# Three small grammars, parsed here rather than in the reminder code so that a
# typo in ``meercal.toml`` fails when the file is read and not at 06:50 on the
# morning the reminder was supposed to fire.


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_RE = re.compile(r"^\s*([+-]?)\s*(\d+)\s*([smhdw])\s*$", re.I)
_ISO_RE = re.compile(
    r"^\s*([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?\s*$", re.I
)
_AT_RE = re.compile(r"^\s*(?:([+-]?\d+)\s*d\s+)?([0-2]?\d):([0-5]\d)\s*$", re.I)
_QUIET_RE = re.compile(r"^\s*([0-2]?\d):([0-5]\d)\s*-\s*([0-2]?\d):([0-5]\d)\s*$")


def parse_duration(text: str) -> timedelta:
    """``10m``, ``1h``, ``2d``, ``-PT15M``: a lead time, as a timedelta.

    Both spellings are accepted because both are already in play: the short one
    is what a person writes in a config file, and the ISO 8601 one is what a
    VALARM TRIGGER arrives as. Returning a signed value keeps the sign where
    iCalendar puts it: ``-PT15M`` means fifteen minutes *before*, and it is the
    reminder code's job to know that a bare ``15m`` means the same thing.
    """
    if not text or not str(text).strip():
        raise ValueError("empty duration")
    raw = str(text).strip()

    m = _DURATION_RE.match(raw)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return timedelta(seconds=sign * int(m.group(2)) * _DURATION_UNITS[m.group(3).lower()])

    m = _ISO_RE.match(raw)
    if m and any(m.group(i) for i in range(2, 7)):
        sign = -1 if m.group(1) == "-" else 1
        weeks, days, hours, mins, secs = (int(m.group(i) or 0) for i in range(2, 7))
        return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=mins, seconds=secs)

    raise ValueError(f"not a duration: {text!r} (try 10m, 2h, 1d, or -PT15M)")


def parse_at(text: str) -> tuple[int, int, int]:
    """``-1d 18:00`` or ``09:00``: an absolute wall-clock anchor.

    Returns ``(day_offset, hour, minute)``. This is what all-day events need and
    what "the evening before" means: a lead time cannot express it, because
    there is no clock on the thing being led away from.
    """
    m = _AT_RE.match(str(text or ""))
    if not m:
        raise ValueError(f"not a time anchor: {text!r} (try '-1d 18:00' or '09:00')")
    hour, minute = int(m.group(2)), int(m.group(3))
    if hour > 23:
        raise ValueError(f"not a time anchor: {text!r} (hour {hour} is not a clock time)")
    return int(m.group(1) or 0), hour, minute


def parse_clock(text: str) -> tuple[int, int]:
    """``09:00``: a wall-clock time of day."""
    offset, hour, minute = parse_at(text)
    if offset:
        raise ValueError(f"not a time of day: {text!r}")
    return hour, minute


def parse_quiet_hours(text: str) -> tuple[int, int] | None:
    """``23:00-07:00`` as two minute-of-day marks, or None for "no quiet hours"."""
    if not text or not str(text).strip():
        return None
    m = _QUIET_RE.match(str(text))
    if not m:
        raise ValueError(f"not a quiet-hours range: {text!r} (try '23:00-07:00')")
    start_h, start_m, end_h, end_m = (int(g) for g in m.groups())
    if start_h > 23 or end_h > 23:
        raise ValueError(f"not a quiet-hours range: {text!r}")
    return start_h * 60 + start_m, end_h * 60 + end_m


def in_quiet_hours(wall: datetime, window: tuple[int, int] | None) -> bool:
    """Is this wall-clock time inside the quiet window?

    The window wraps midnight far more often than it does not (23:00-07:00 is
    the shape everybody writes) so the wrapping case is the one written first.
    """
    if window is None:
        return False
    start, end = window
    minute = wall.hour * 60 + wall.minute
    if start == end:
        return False
    if start > end:  # wraps midnight: 23:00-07:00
        return minute >= start or minute < end
    return start <= minute < end


def humanize_lead(delta: timedelta) -> str:
    """``in 10 minutes``, ``now``, ``12 minutes ago``: for the notification text.

    Said the way a person would say it. Two rules do all the work: an exact
    multiple of a unit uses that unit, so an hour is "1 hour" and not "60
    minutes"; anything else drops to the largest unit it has at least two of, so
    ninety minutes is "90 minutes" and not the "2 hours" that plain rounding
    would produce and that would send you to the wrong meeting.
    """
    secs = int(delta.total_seconds())
    if -60 < secs < 60:
        return "now"
    past = secs < 0
    secs = abs(secs)

    def said(n: int, unit: str) -> str:
        label = f"{n} {unit}{'s' if n != 1 else ''}"
        return f"{label} ago" if past else f"in {label}"

    # Only days and hours get the exact-multiple treatment. Including minutes
    # here would catch *every* whole-minute span, and two days ago would be
    # reported as 2910 minutes ago.
    for unit, size in (("day", 86400), ("hour", 3600)):
        if secs >= size and secs % size == 0:
            return said(secs // size, unit)
    for unit, size in (("day", 86400), ("hour", 3600)):
        if secs >= 2 * size:
            return said(round(secs / size), unit)
    return said(max(1, round(secs / 60)), "minute")
