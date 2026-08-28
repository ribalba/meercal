"""iCalendar in, plain values out.

Everything that knows about RFC 5545 lives here, so that the rest of the
program deals in datetimes and strings. The agent parses what a server sends;
the server parses what a user pastes or a mail carries. Same code, because an
invitation in an email and an event on a CalDAV server are the same object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from icalendar import Calendar as ICalendar
from icalendar.prop import vDuration

from ..timeutil import UTC, to_utc, zone

# What a VEVENT with neither DTEND nor DURATION means. RFC 5545 says a timed
# one takes no time at all and an all-day one takes its day; both are drawn,
# and a zero-length event is a legitimate marker rather than a parse failure.
_DEFAULT_TIMED_DURATION = timedelta(0)


@dataclass
class ParsedEvent:
    """One VEVENT, in the shapes core.models stores."""

    uid: str
    recurrence_id: str = ""
    summary: str = ""
    description: str = ""
    location: str = ""
    status: str = ""
    transparent: bool = False
    all_day: bool = False
    dtstart: datetime = datetime(1970, 1, 1)      # naive UTC
    dtend: datetime = datetime(1970, 1, 1)        # naive UTC, exclusive
    dtstart_local: datetime = datetime(1970, 1, 1)  # wall time in tz_id
    tz_id: str = "UTC"
    duration_s: int = 0
    rrule: str = ""
    rdate: str = ""
    exdate: str = ""
    organizer: str = ""
    attendees: list[dict] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    alarms: list[dict] = field(default_factory=list)
    sequence: int = 0
    raw_ics: str = ""

    @property
    def search_text(self) -> str:
        people = " ".join(
            f"{a.get('name', '')} {a.get('email', '')}".strip() for a in self.attendees
        )
        return " ".join(
            p for p in (self.summary, self.location, self.description, self.organizer, people) if p
        )


def _text(comp, key: str) -> str:
    value = comp.get(key)
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # a property this library models as bytes
        return ""


def _tz_name(value: Any, param_tz: str, default_tz: str) -> str:
    """Which zone a DTSTART is stated in.

    The TZID parameter is the authority when it is there. Without one the value
    is either UTC (a trailing Z, which icalendar has already resolved) or
    *floating*: a wall time with no zone, which RFC 5545 says happens in
    whatever zone the reader is in. Floating gets the calendar's own zone,
    because "the calendar's timezone" is the closest thing to the reader that a
    background sync has.
    """
    if param_tz:
        return param_tz
    tzinfo = getattr(value, "tzinfo", None)
    if tzinfo is None:
        return default_tz or "UTC"
    key = getattr(tzinfo, "key", None)
    if key:
        return key
    return "UTC" if tzinfo.utcoffset(None) in (timedelta(0), None) else default_tz or "UTC"


def _split(value: Any, tz_id: str) -> tuple[datetime, datetime, str, bool]:
    """A DTSTART/DTEND value as (utc, wall, tz_id, all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            wall = value.astimezone(zone(tz_id)).replace(tzinfo=None)
            utc = value.astimezone(UTC).replace(tzinfo=None)
        else:
            wall = value
            utc = to_utc(wall, tz_id)
        return utc, wall, tz_id, False
    if isinstance(value, date):
        midnight = datetime.combine(value, time.min)
        # Dates are not converted. See core/timeutil.
        return midnight, midnight, "UTC", True
    raise ValueError(f"unsupported date value {value!r}")


def _dates_as_wall(prop, tz_id: str, all_day: bool) -> list[str]:
    """EXDATE/RDATE flattened to the wall-time ISO strings core.expand reads."""
    items = prop if isinstance(prop, list) else [prop]
    out: list[str] = []
    for item in items:
        for dt in getattr(item, "dts", []):
            try:
                _, wall, _, _ = _split(dt.dt, tz_id)
            except ValueError:
                continue
            out.append(wall.isoformat())
    return out


# ATTENDEE parameters this program models by name; everything else is carried
# through untouched in the person's "params".
_OWN_PARAMS = {"CN", "PARTSTAT", "ROLE"}


def _people(comp) -> tuple[str, list[dict]]:
    def address(value) -> str:
        return str(value).replace("mailto:", "").replace("MAILTO:", "").strip()

    organizer = ""
    if comp.get("ORGANIZER") is not None:
        organizer = address(comp.get("ORGANIZER"))

    raw = comp.get("ATTENDEE")
    if raw is None:
        return organizer, []
    items = raw if isinstance(raw, list) else [raw]
    attendees = []
    for item in items:
        params = getattr(item, "params", {})
        person = {
            "email": address(item),
            "name": str(params.get("CN", "")),
            "status": str(params.get("PARTSTAT", "NEEDS-ACTION")),
            "role": str(params.get("ROLE", "REQ-PARTICIPANT")),
        }
        # The panel rewrites this property wholesale when someone is added or
        # removed, so anything it cannot name is anything it would throw away:
        # CUTYPE=RESOURCE is how a room says it is a room, RSVP and
        # DELEGATED-TO carry the rest of a scheduling exchange. Kept verbatim
        # and handed straight back by core.cal.build. The key is left off
        # entirely when there is nothing extra, so the common attendee stays
        # the four plain fields it has always been.
        extra = {k: str(v) for k, v in params.items() if k.upper() not in _OWN_PARAMS}
        if extra:
            person["params"] = extra
        attendees.append(person)
    return organizer, attendees


def _alarms(comp) -> list[dict]:
    """The VEVENT's own VALARMs, as ``{trigger, related, action, description}``.

    Kept because a reminder rule can defer to them (``lead = "valarm"``), which
    is what lets meercal agree with the alarm your phone already set instead of
    arguing with it. Two notifications for one meeting, five minutes apart, is
    how people end up switching reminders off entirely.

    The trigger is normalised back to its ISO 8601 spelling (``-PT15M``)
    whichever way icalendar handed it over. An absolute trigger is kept too,
    with ``related`` set to ``"ABSOLUTE"``, so that nothing is silently dropped
    on the way in; the reminder code decides what it can use.
    """
    out: list[dict] = []
    for alarm in comp.walk("VALARM"):
        trigger = alarm.get("TRIGGER")
        if trigger is None:
            continue
        value = getattr(trigger, "dt", None)
        related = str(trigger.params.get("RELATED", "START") or "START").upper()
        if isinstance(value, timedelta):
            text = vDuration(value).to_ical().decode("ascii")
        elif isinstance(value, datetime):
            text, related = value.isoformat(), "ABSOLUTE"
        else:
            continue
        out.append(
            {
                "trigger": text,
                "related": related,
                "action": _text(alarm, "ACTION").upper() or "DISPLAY",
                "description": _text(alarm, "DESCRIPTION"),
            }
        )
    return out


def parse_event(comp, default_tz: str = "UTC") -> ParsedEvent | None:
    """One VEVENT component. Returns None for anything without a usable start:
    a VEVENT with no DTSTART is not an event, it is a bug on the other end."""
    from .. import expand  # local import: expand imports timeutil, not this

    dtstart_prop = comp.get("DTSTART")
    if dtstart_prop is None:
        return None
    uid = _text(comp, "UID")
    if not uid:
        return None

    tz_param = str(dtstart_prop.params.get("TZID", "") or "")
    tz_id = _tz_name(dtstart_prop.dt, tz_param, default_tz)
    try:
        start_utc, start_wall, tz_id, all_day = _split(dtstart_prop.dt, tz_id)
    except ValueError:
        return None

    dtend_prop = comp.get("DTEND")
    if dtend_prop is not None:
        try:
            end_utc, _, _, _ = _split(dtend_prop.dt, tz_id)
        except ValueError:
            end_utc = start_utc
    elif comp.get("DURATION") is not None:
        end_utc = start_utc + comp.get("DURATION").dt
    else:
        end_utc = start_utc + (timedelta(days=1) if all_day else _DEFAULT_TIMED_DURATION)
    if end_utc < start_utc:
        end_utc = start_utc  # servers do send these; a negative length draws nothing

    rid = ""
    rid_prop = comp.get("RECURRENCE-ID")
    if rid_prop is not None:
        try:
            _, rid_wall, _, rid_all_day = _split(rid_prop.dt, tz_id)
            rid = expand.recurrence_key(rid_wall, rid_all_day)
        except ValueError:
            rid = ""

    rrule_prop = comp.get("RRULE")
    if rrule_prop is None:
        rrule = ""
    else:
        rules = rrule_prop if isinstance(rrule_prop, list) else [rrule_prop]
        rrule = "\n".join(r.to_ical().decode("utf-8", "replace") for r in rules)

    exdate = ",".join(_dates_as_wall(comp["EXDATE"], tz_id, all_day)) if comp.get("EXDATE") else ""
    rdate = ",".join(_dates_as_wall(comp["RDATE"], tz_id, all_day)) if comp.get("RDATE") else ""

    organizer, attendees = _people(comp)
    categories: list[str] = []
    if comp.get("CATEGORIES") is not None:
        cats = comp.get("CATEGORIES")
        cats = cats if isinstance(cats, list) else [cats]
        for c in cats:
            categories.extend(str(x) for x in getattr(c, "cats", []) or [str(c)])

    try:
        sequence = int(comp.get("SEQUENCE", 0) or 0)
    except (TypeError, ValueError):
        sequence = 0

    return ParsedEvent(
        uid=uid,
        recurrence_id=rid,
        summary=_text(comp, "SUMMARY"),
        description=_text(comp, "DESCRIPTION"),
        location=_text(comp, "LOCATION"),
        status=_text(comp, "STATUS").upper(),
        transparent=_text(comp, "TRANSP").upper() == "TRANSPARENT",
        all_day=all_day,
        dtstart=start_utc,
        dtend=end_utc,
        dtstart_local=start_wall,
        tz_id=tz_id,
        duration_s=int((end_utc - start_utc).total_seconds()),
        rrule=rrule,
        rdate=rdate,
        exdate=exdate,
        organizer=organizer,
        attendees=attendees,
        categories=categories,
        alarms=_alarms(comp),
        sequence=sequence,
        raw_ics=comp.to_ical().decode("utf-8", "replace"),
    )


def parse_calendar(text: str, default_tz: str = "UTC") -> list[ParsedEvent]:
    """Every VEVENT in one iCalendar document.

    A CalDAV resource usually holds one series (the master and its overrides)
    and an .ics subscription holds the whole calendar. Both come through here.
    """
    if not text or "BEGIN:VCALENDAR" not in text:
        return []
    try:
        cal = ICalendar.from_ical(text)
    except Exception:
        return []
    tz_id = str(cal.get("X-WR-TIMEZONE", "") or "") or default_tz
    out = []
    for comp in cal.walk("VEVENT"):
        parsed = parse_event(comp, default_tz=tz_id)
        if parsed is not None:
            out.append(parsed)
    return out


def calendar_name(text: str) -> str:
    """The display name an .ics feed gives itself, if any."""
    try:
        cal = ICalendar.from_ical(text)
    except Exception:
        return ""
    return str(cal.get("X-WR-CALNAME", "") or "")
