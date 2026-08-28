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
        if person.get("role"):
            params["ROLE"] = person["role"]
        # Whatever else the server had on that line: CUTYPE=RESOURCE is how a
        # room says it is a room, and DELEGATED-TO is half of a delegation that
        # means nothing without its other half. The panel owns this property
        # now (see _OWNED), so anything not carried here is anything lost.
        params.update(person.get("params") or {})
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
_PATCHABLE = ("SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND", "RRULE",
              "STATUS", "TRANSP", "ATTENDEE")

# Of those, the ones the panel owns *completely*: the original lines go even
# when the edit has none to put back. Every other patchable property is
# replaced only when there is something to replace it with, so an empty
# LOCATION leaves the server's alone -- but an invitation with nobody on it is
# a real answer, and it is the only way removing the last guest can stick.
_OWNED = ("ATTENDEE",)


def _unfold(text: str) -> list[str]:
    """The logical lines of an iCalendar body.

    RFC 5545 folds anything past 75 octets onto a continuation line beginning
    with a space, and a patch that matches property names against *physical*
    lines does not see that: the tail of a folded DESCRIPTION looks like a line
    of its own, so replacing the property drops the head and leaves the orphan
    glued to the front of whatever comes next. Long descriptions and ATTENDEE
    lines carrying a CN both fold as a matter of course, so this is the common
    case and not the edge one.
    """
    lines: list[str] = []
    for line in text.splitlines():
        if lines and line[:1] in (" ", "\t"):
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _fold(line: str) -> list[str]:
    """One logical line back into physical ones, counted the way the spec
    counts: 75 octets, the continuation's leading space included, and never a
    cut through the middle of a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, first = [], True
    while raw:
        limit = 75 if first else 74
        cut = min(limit, len(raw))
        while cut < len(raw) and (raw[cut] & 0xC0) == 0x80:   # mid-character
            cut -= 1
        if cut <= 0:                                          # nothing else fits
            cut = min(limit, len(raw))
        out.append(("" if first else " ") + raw[:cut].decode("utf-8", "replace"))
        raw = raw[cut:]
        first = False
    return out


def _value(line: str) -> str:
    """Everything after a content line's name and parameters.

    The first colon, but not one inside a quoted parameter: `CN="Meier: Anna"`
    is legal and its colon is part of the name, not the separator.
    """
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[i + 1:]
    return ""


def _guest(line: str) -> str:
    """Who an ATTENDEE line is about, as a key: the address, lowercased."""
    return re.sub(r"^mailto:", "", _value(line).strip(), flags=re.I).lower()


def patch_ics(raw: str, event: Event) -> str:
    """The stored text with this event's edited properties written into it.

    Falls back to building from scratch when there is no usable original, which
    is the case for anything meercal created itself.
    """
    if not raw or "BEGIN:VEVENT" not in raw:
        return event_to_ics(event)
    fresh = ICalendar.from_ical(event_to_ics(event))
    replacement: dict[str, list[str]] = {name: [] for name in _OWNED}
    for comp in fresh.walk("VEVENT"):
        for line in _unfold(comp.to_ical().decode("utf-8", "replace")):
            name = re.split(r"[;:]", line, maxsplit=1)[0]
            if name in _PATCHABLE:
                replacement.setdefault(name, []).append(line)

    # ATTENDEE is rebuilt rather than overwritten. The panel decides *who* is on
    # the invitation; the server decides what each of them said back. So a guest
    # the original already had keeps their line exactly as it arrived -- PARTSTAT,
    # DELEGATED-TO, X- parameters and all -- and only somebody genuinely new gets
    # a line written here. Without this, opening an event before an acceptance had
    # synced down and saving an unrelated field would put NEEDS-ACTION back over
    # the "yes" the server already had.
    if "ATTENDEE" in replacement:
        already = {}
        for line in _unfold(raw):
            if re.split(r"[;:]", line, maxsplit=1)[0] == "ATTENDEE":
                already[_guest(line)] = line
        replacement["ATTENDEE"] = [
            already.get(_guest(line), line) for line in replacement["ATTENDEE"]
        ]

    out, seen = [], set()
    for line in _unfold(raw):
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
    lines = [phys for line in out for phys in _fold(line)]
    if "BEGIN:VCALENDAR" not in raw:
        # What the database holds is the VEVENT on its own -- core.cal.parse
        # stores one component per row -- and a PUT of a bare component is not
        # iCalendar at all. Google answers 400; a more forgiving server accepts
        # something it should not. Put the envelope back.
        lines = ["BEGIN:VCALENDAR", f"PRODID:{PRODID}", "VERSION:2.0", *lines, "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
