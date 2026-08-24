"""Events back out as iCalendar, for the agent to PUT.

Round-tripping through a parse and a re-serialise loses every property this
program does not model: alarms, attachments, X- properties a phone put there.
So an event that *came from a server* keeps its original text in
``raw_ics`` and is patched line by line; only an event created here is built
from nothing. That is the difference between editing a calendar and rewriting
it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from ..models import Event
from ..timeutil import utcnow, zone

PRODID = "-//meercal//EN"


def new_uid() -> str:
    # The @meercal suffix is not decoration: a UID collides across a whole
    # account, and servers do reject a PUT whose UID already exists elsewhere.
    return f"{uuid.uuid4()}@meercal"


def _stamp(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def _vevent(event: Event) -> IEvent:
    ve = IEvent()
    ve.add("uid", event.uid)
    ve.add("dtstamp", _stamp(utcnow()))
    ve.add("sequence", event.sequence)
    ve.add("summary", event.summary)
    if event.description:
        ve.add("description", event.description)
    if event.location:
        ve.add("location", event.location)
    if event.status:
        ve.add("status", event.status)
    if event.transparent:
        ve.add("transp", "TRANSPARENT")

    if event.all_day:
        # VALUE=DATE on both ends, and DTEND exclusive: the day after the last
        # day. icalendar writes the DATE form for a `date`, which is why the
        # conversion here is to `.date()` and not to a midnight datetime.
        ve.add("dtstart", event.dtstart.date())
        ve.add("dtend", (event.dtstart + timedelta(seconds=event.duration_s)).date())
    else:
        tz = zone(event.tz_id)
        start = event.dtstart_local.replace(tzinfo=tz)
        ve.add("dtstart", start)
        ve.add("dtend", start + timedelta(seconds=event.duration_s))

    for rule in event.rrule.splitlines():
        if rule.strip():
            ve.add("rrule", _vrecur(rule.strip()))
    if event.organizer:
        ve.add("organizer", f"mailto:{event.organizer}")
    for person in event.attendees:
        email = person.get("email", "")
        if not email:
            continue
        params = {"CN": person.get("name", "") or email, "PARTSTAT": person.get("status", "NEEDS-ACTION")}
        ve.add("attendee", f"mailto:{email}", parameters=params)
    return ve


def _vrecur(text: str) -> dict:
    """`FREQ=WEEKLY;BYDAY=MO,TU` as the dict icalendar wants."""
    out: dict[str, object] = {}
    for part in text.replace("RRULE:", "").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip().upper()] = value.split(",") if "," in value else value
    return out


def event_to_ics(event: Event) -> str:
    """A whole VCALENDAR carrying this event."""
    cal = ICalendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add_component(_vevent(event))
    return cal.to_ical().decode("utf-8", "replace")


# Properties an edit here may replace in text the server sent. Anything not
# listed is left exactly as it arrived.
_PATCHABLE = ("SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND", "RRULE", "STATUS", "TRANSP")


def patch_ics(raw: str, event: Event) -> str:
    """The stored text with this event's edited properties written into it.

    Falls back to building from scratch when there is no usable original, which
    is the case for anything meercal created itself.
    """
    if not raw or "BEGIN:VEVENT" not in raw:
        return event_to_ics(event)
    fresh = ICalendar.from_ical(event_to_ics(event))
    replacement = {}
    for comp in fresh.walk("VEVENT"):
        for line in comp.to_ical().decode("utf-8", "replace").splitlines():
            name = re.split(r"[;:]", line, maxsplit=1)[0]
            if name in _PATCHABLE:
                replacement.setdefault(name, []).append(line)

    out, seen = [], set()
    for line in raw.splitlines():
        name = re.split(r"[;:]", line, maxsplit=1)[0]
        if name in replacement:
            if name not in seen:
                out.extend(replacement[name])
                seen.add(name)
            continue  # drop the original, patched or not
        out.append(line)
    # Properties the original did not have at all (a location added here).
    if "END:VEVENT" in raw:
        missing = [ln for name, lines in replacement.items() if name not in seen for ln in lines]
        if missing:
            idx = next(i for i, ln in enumerate(out) if ln.startswith("END:VEVENT"))
            out[idx:idx] = missing
    return "\r\n".join(out) + "\r\n"
