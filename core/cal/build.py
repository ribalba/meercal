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
from datetime import date, datetime, timedelta

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar.prop import vRecur

from ..models import Event
from ..timeutil import UTC, utcnow, zone
from .parse import ParsedEvent, parse_calendar

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

    if event.recurrence_id:
        # Which instance of the series this row replaces, stated in the zone
        # DTSTART is written in just above. The server matches it against the
        # master's instances by instant, so the zone is free to choose and the
        # instant is not.
        rid = _recurrence_value(event.recurrence_id, _key_zone(event), event.tz_id)
        if rid is not None:
            ve.add("recurrence-id", rid)

    for rule in event.rrule.splitlines():
        if rule.strip():
            ve.add("rrule", _vrecur(rule.strip()))
    if event.organizer:
        ve.add("organizer", _cal_address(event.organizer))
    for person in event.attendees:
        email = person.get("email", "")
        if not email:
            continue
        params = {"CN": person.get("name", "") or email, "PARTSTAT": person.get("status", "NEEDS-ACTION")}
        if person.get("role"):
            params["ROLE"] = person["role"]
        # An invitation asks for an answer, and RSVP=TRUE is how it asks: Apple
        # and Google both put it on every guest they invite. A guest who has
        # answered has nothing left to be asked, and an event nobody organises
        # is not an invitation at all, whoever is listed on it.
        if event.organizer and params["PARTSTAT"] == "NEEDS-ACTION":
            params["RSVP"] = "TRUE"
        # Whatever else the server had on that line: CUTYPE=RESOURCE is how a
        # room says it is a room, and DELEGATED-TO is half of a delegation that
        # means nothing without its other half. The panel owns this property
        # now (see _OWNED), so anything not carried here is anything lost.
        params.update(person.get("params") or {})
        ve.add("attendee", _cal_address(email), parameters=params)
    return ve


def _cal_address(value: str) -> str:
    """A person as a calendar user address: an email as ``mailto:``, and a
    server's own form left alone.

    RFC 6638 scheduling turns on the ORGANIZER, and a server runs it only for
    an organiser it recognises as the account itself, so what goes here has to
    be an address the server knows. iCloud names its users by principal URL
    (``/aNDE0.../principal/``) and core.cal.parse keeps that as it came;
    ``mailto:`` in front of a path is an address nobody has.
    """
    value = value.strip()
    if value.startswith("/") or ":" in value.split("@", 1)[0]:
        return value
    return f"mailto:{value}"


def _vrecur(text: str) -> vRecur:
    """`FREQ=WEEKLY;BYDAY=MO,TU` as the value icalendar wants.

    Parsed by icalendar itself rather than split by hand: UNTIL has to arrive as
    a date or a datetime, and a string there is a TypeError at serialise time.
    That is every bounded series, and the error surfaced in the agent's queue,
    not here, so an edited or imported course simply never reached the server.
    """
    return vRecur.from_ical(text.strip().removeprefix("RRULE:"))


def recurrence_wall(key: str) -> tuple[datetime, bool] | None:
    """A recurrence key (see core.expand.recurrence_key) as (wall time, all day).

    The key is the only record of which instance an override replaces, so this
    is the one way back from it: a timed key is a wall time in the event's own
    zone, an all-day key is a date and is kept as its midnight.
    """
    for fmt, all_day in (("%Y%m%dT%H%M%S", False), ("%Y%m%d", True)):
        try:
            return datetime.strptime(key, fmt), all_day
        except ValueError:
            continue
    return None


def _key_zone(event: Event) -> str:
    """The zone an override's recurrence key is a wall time in.

    core.cal.parse works the key out in the zone of the DTSTART it arrived
    with, and that zone is still in ``raw_ics``. It is not always ``tz_id``: an
    edit in the panel restates the row in the display zone and leaves the key
    as it was, so a meeting moved in New York and edited in Berlin would
    otherwise name the instance at 09:30 Berlin, six hours from the one it
    replaces.
    """
    raw = event.raw_ics or ""
    if "BEGIN:VEVENT" in raw:
        if not _is_calendar(raw):
            raw = f"BEGIN:VCALENDAR\r\n{raw}\r\nEND:VCALENDAR\r\n"
        parsed = parse_calendar(raw, default_tz=event.tz_id or "UTC")
        if len(parsed) == 1:
            return parsed[0].tz_id
    return event.tz_id


def _recurrence_value(key: str, key_tz: str, tz_id: str) -> datetime | date | None:
    """A recurrence key, a wall time in ``key_tz``, as the value a RECURRENCE-ID
    stated in ``tz_id`` carries. A date stays a date."""
    parsed = recurrence_wall(key)
    if parsed is None:
        return None
    wall, all_day = parsed
    if all_day:
        return wall.date()
    return wall.replace(tzinfo=zone(key_tz)).astimezone(zone(tz_id))


def event_to_ics(event: Event) -> str:
    """A whole VCALENDAR carrying this event."""
    cal = ICalendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add_component(_vevent(event))
    return cal.to_ical().decode("utf-8", "replace")


# Properties an edit here may replace in text the server sent. Anything not
# listed is left exactly as it arrived.
#
# RECURRENCE-ID is here so that an override goes out stated in the same zone as
# the DTSTART and DTEND written beside it. Only an override has one to put back,
# and a property with no replacement is left alone, so a master is untouched.
_PATCHABLE = ("SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND", "RRULE",
              "STATUS", "TRANSP", "ATTENDEE", "RECURRENCE-ID", "ORGANIZER")

# Of those, the ones the panel owns *completely*: the original lines go even
# when the edit has none to put back. Every other patchable property is
# replaced only when there is something to replace it with, so an empty
# LOCATION leaves the server's alone -- but an invitation with nobody on it is
# a real answer, and it is the only way removing the last guest can stick.
_OWNED = ("ATTENDEE",)

# And the one that is only ever *added*: written into an original that has
# none, never over one that has. A server keeps the organiser in a form of its
# own (iCloud: a principal URL, with the address in an EMAIL parameter beside
# it), and once an invitation is out the line says whose it is. No server lets
# a client hand it to somebody else by rewriting it, and rewriting the same
# person in a different spelling is a bug waiting to be found. So the line
# stays exactly as it arrived, and only an event that had none gets one, which
# is what turns a guest list nobody mails into an invitation (see
# app.routers.events._organise).
_ADDED_ONLY = ("ORGANIZER",)


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

    original = _unfold(raw)
    present = {re.split(r"[;:]", line, maxsplit=1)[0] for line in original}
    for name in _ADDED_ONLY:
        if name in present:
            replacement.pop(name, None)

    out, seen = [], set()
    for line in original:
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


def has_organizer(ics: str) -> bool:
    """Does any component of this text carry an ORGANIZER?"""
    return any(re.split(r"[;:]", line, maxsplit=1)[0] == "ORGANIZER" for line in _unfold(ics))


def without_guests(ics: str, organizer: str) -> str:
    """The same text with every ATTENDEE but the organiser's own line taken out.

    The first half of turning a plain event into an invitation on a server
    that is already holding it; see agent.sync._apply_action for why there
    are two halves.
    """
    me = re.sub(r"^mailto:", "", organizer.strip(), flags=re.I).lower()
    kept = [
        line for line in _unfold(ics)
        if re.split(r"[;:]", line, maxsplit=1)[0] != "ATTENDEE" or _guest(line) == me
    ]
    return "\r\n".join(phys for line in kept for phys in _fold(line)) + "\r\n"


# --- whole resources ---------------------------------------------------------
#
# A CalDAV resource is not an event. A recurring series and every instance moved
# out of it share a UID, and RFC 4791 keeps all of them in one resource: one URL,
# one etag, one VCALENDAR holding the master and each override as a VEVENT of its
# own. The database keeps a row per VEVENT, so a row is only ever a *part* of
# what the agent has to PUT, and a PUT replaces the whole. Writing one row's
# VCALENDAR over that URL deleted every moved instance when the series was
# edited, and the series itself when one moved instance was. What follows is the
# other way round: take the resource as the server holds it, and change exactly
# one VEVENT in it.

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _logical_lines(text: str) -> list[list[str]]:
    """The body as logical lines, each kept as the physical lines it came in.

    ``_unfold`` joins the continuations, which is what matching wants. Splicing
    wants something else as well: to hand back everything it did not change as
    the server wrote it, folds included, so the physical lines travel with the
    logical one. Blank lines are dropped; they are not iCalendar, and servers do
    append them. So is a byte order mark, which is not a line of anything.
    """
    out: list[list[str]] = []
    for line in _LINE_BREAK.split((text or "").lstrip("\ufeff")):
        if out and line[:1] in (" ", "\t"):
            out[-1].append(line)
        elif line.strip():
            out.append([line])
    return out


def _joined(group: list[str]) -> str:
    return group[0] + "".join(line[1:] for line in group[1:])


def _head(group: list[str]) -> str:
    return _joined(group).strip().upper()


def _serialise(groups) -> str:
    return "\r\n".join(line for group in groups for line in group) + "\r\n"


def _pieces(text: str) -> list[tuple[str, list[list[str]]]]:
    """A calendar cut into what sits directly inside its VCALENDAR, in order.

    Each piece is ``(kind, lines)``. A component (a VEVENT with its VALARMs, a
    VTIMEZONE with its rules) is one piece, named after the component, BEGIN
    and END included. Every line of the calendar itself (BEGIN:VCALENDAR,
    PRODID, METHOD, END:VCALENDAR) is a piece of its own, of kind ``""``.
    """
    pieces: list[tuple[str, list[list[str]]]] = []
    depth = 0
    for group in _logical_lines(text):
        head = _head(group)
        if head.startswith("BEGIN:"):
            depth += 1
            if depth == 2:
                pieces.append((head[len("BEGIN:"):].strip(), [group]))
                continue
        elif head.startswith("END:") and depth > 0:
            depth -= 1
            if depth == 1:
                pieces[-1][1].append(group)
                continue
        if depth >= 2:
            pieces[-1][1].append(group)
        else:
            pieces.append(("", [group]))
    return pieces


def _is_calendar(text: str | None) -> bool:
    return bool(re.search(r"^\ufeff?BEGIN:VCALENDAR\s*$", text or "", re.I | re.M))


def _envelope(pieces: list[tuple[str, list[list[str]]]]) -> list[list[str]]:
    """What one VEVENT needs around it to be read the way its whole resource is.

    The calendar's own properties, because an X-WR-TIMEZONE changes what a
    floating time means, and every VTIMEZONE, so that a TZID resolves to the
    offsets the server wrote down beside it rather than to nothing at all.
    """
    out: list[list[str]] = []
    for kind, groups in pieces:
        if kind == "VTIMEZONE":
            out.extend(groups)
        elif kind == "" and _head(groups[0]) not in ("BEGIN:VCALENDAR", "END:VCALENDAR"):
            out.extend(groups)
    return out


def _standalone(envelope: list[list[str]], block: list[list[str]]) -> str:
    return _serialise([["BEGIN:VCALENDAR"], *envelope, *block, ["END:VCALENDAR"]])


def _read(envelope: list[list[str]], block: list[list[str]], default_tz: str) -> ParsedEvent | None:
    """One VEVENT of a resource, parsed exactly as ingest parses it.

    That is the whole point of going through core.cal.parse rather than
    comparing RECURRENCE-ID lines as text: the server spells the zone its own
    way (Exchange writes ``TZID=W. Europe Standard Time``), meercal writes the
    IANA name, and one instance spelled two ways has to be one instance, or a
    splice adds a second copy of it next to the first. The recurrence key a row
    was stored under is the one this produces for the same component.
    """
    parsed = parse_calendar(_standalone(envelope, block), default_tz=default_tz)
    return parsed[0] if len(parsed) == 1 else None


def _first_vevent(text: str) -> list[list[str]]:
    if not _is_calendar(text):
        text = f"BEGIN:VCALENDAR\r\n{text}\r\nEND:VCALENDAR\r\n"
    for kind, groups in _pieces(text):
        if kind == "VEVENT":
            return groups
    raise ValueError("there is no VEVENT in the component to splice in")


def splice_ics(
    resource: str,
    uid: str,
    recurrence_id: str,
    component: str | None,
    default_tz: str = "UTC",
) -> str:
    """``resource`` with one VEVENT in it replaced, added or taken out.

    ``component`` is what ``patch_ics`` or ``event_to_ics`` returns, a
    VCALENDAR around one VEVENT, and that VEVENT is what goes in: in place of
    the one with this UID and recurrence key (``""`` is the master), or at the
    end when the resource has no such VEVENT yet. ``None`` takes it out instead.

    Everything else comes back as the server sent it, byte for byte apart from
    the line endings, which are CRLF: the calendar's properties, its
    VTIMEZONEs, every other VEVENT and whatever alarms and X- properties those
    carry. ``default_tz`` is the calendar's zone, the one a floating time is
    read in, and has to be the one ingest used for the keys to agree (see
    core.cal.ingest.store_resource).

    A resource holding the same instance twice, which no server should allow,
    comes back holding it once, as storing it makes one row of the two. A body
    that is not iCalendar at all is refused with ValueError: written over, it
    would be replaced by less than it held.
    """
    block = _first_vevent(component) if component is not None else None
    if not (resource or "").strip():
        if block is None:
            return ""
        return _serialise([["BEGIN:VCALENDAR"], [f"PRODID:{PRODID}"], ["VERSION:2.0"],
                           *block, ["END:VCALENDAR"]])
    if not _is_calendar(resource):
        raise ValueError("the resource is not iCalendar; not writing over it")

    pieces = _pieces(resource)
    envelope = _envelope(pieces)
    out: list[list[str]] = []
    placed = block is None
    for kind, groups in pieces:
        if kind == "VEVENT":
            found = _read(envelope, groups, default_tz)
            if found is not None and (found.uid, found.recurrence_id) == (uid, recurrence_id):
                if not placed:
                    out.extend(block)
                    placed = True
                continue
        if not placed and kind == "" and _head(groups[0]) == "END:VCALENDAR":
            out.extend(block)
            placed = True
        out.extend(groups)
    if not placed:  # a resource cut off before its END:VCALENDAR
        out.extend(block)
    return _serialise(out)


def vevent_text(resource: str, uid: str, recurrence_id: str, default_tz: str = "UTC") -> str:
    """The one VEVENT of ``resource`` with this UID and recurrence key, as text
    of its own (the shape ``Event.raw_ics`` holds), or ``""`` if there is none."""
    if not _is_calendar(resource):
        return ""
    pieces = _pieces(resource)
    envelope = _envelope(pieces)
    for kind, groups in pieces:
        if kind != "VEVENT":
            continue
        found = _read(envelope, groups, default_tz)
        if found is not None and (found.uid, found.recurrence_id) == (uid, recurrence_id):
            return _serialise(groups)
    return ""


def has_vevent(resource: str) -> bool:
    """Whether anything is left in a resource worth keeping it for."""
    return any(kind == "VEVENT" for kind, _ in _pieces(resource or ""))


def add_exdate(resource: str, uid: str, recurrence_id: str, default_tz: str = "UTC") -> str:
    """``resource`` with the instance ``recurrence_id`` excluded from its master.

    What deleting one moved instance has to mean on the server. Taking the
    override out on its own puts the instance back where the series says it
    belongs, which is a delete that reads as "moved the meeting back"; the
    master has to say that the slot is empty.

    The EXDATE is written the way the master writes its DTSTART: the same TZID
    parameter, VALUE=DATE for an all-day series, a UTC time with its Z for a
    series stated in UTC, a floating time for a floating one. RFC 5545 wants an
    EXDATE of the same value type as DTSTART, and a server matching it against
    the instances has the least to get wrong when it is in the same zone too.
    A resource with no master, or an instance already excluded, comes back as
    it was.
    """
    key = recurrence_wall(recurrence_id) if recurrence_id else None
    if key is None or not _is_calendar(resource):
        return _serialise(_logical_lines(resource))

    pieces = _pieces(resource)
    envelope = _envelope(pieces)
    out: list[list[str]] = []
    for kind, groups in pieces:
        if kind == "VEVENT":
            master = _read(envelope, groups, default_tz)
            if master is not None and master.uid == uid and not master.recurrence_id:
                groups = _excluded(envelope, groups, master, *key)
        out.extend(groups)
    return _serialise(out)


def _own_property(block: list[list[str]], name: str) -> str:
    """A component's own ``name`` line, unfolded, not one of a nested VALARM's."""
    depth = 0
    for group in block[1:-1]:
        logical = _joined(group)
        head = logical.strip().upper()
        if head.startswith("BEGIN:"):
            depth += 1
        elif head.startswith("END:"):
            depth -= 1
        elif depth == 0 and re.split(r"[;:]", logical, maxsplit=1)[0].strip().upper() == name:
            return logical
    return ""


def _excluded(
    envelope: list[list[str]],
    block: list[list[str]],
    master: ParsedEvent,
    wall: datetime,
    all_day_key: bool,
) -> list[list[str]]:
    """The master's VEVENT with an EXDATE for ``wall`` added, before any VALARM:
    RFC 5545 has a component's properties ahead of its subcomponents."""
    stamp = "%Y%m%dT%H%M%S"
    if master.all_day:
        wall = datetime(wall.year, wall.month, wall.day)
        line = f"EXDATE;VALUE=DATE:{wall:%Y%m%d}"
    else:
        if all_day_key:  # a date for an instance of a timed series: at its usual time
            wall = datetime.combine(wall.date(), master.dtstart_local.time())
        dtstart = _own_property(block, "DTSTART")
        value = _value(dtstart).strip()
        params = dtstart[: len(dtstart) - len(_value(dtstart)) - 1]
        tzid = re.search(r';TZID=("[^"]*"|[^;:]*)', params, re.I)
        start = None
        try:
            start = ICalendar.from_ical(_standalone(envelope, block)).walk("VEVENT")[0]["DTSTART"].dt
        except Exception:  # parsed once already; this is only after its tzinfo
            pass
        # The key is a wall time in the zone core.cal.parse resolved the TZID
        # to. Where that is not the zone the VTIMEZONE describes (a name this
        # host does not know falls back to UTC), the instant is carried across.
        instant = wall.replace(tzinfo=zone(master.tz_id))
        if value.upper().endswith("Z"):
            line = f"EXDATE:{instant.astimezone(UTC):{stamp}}Z"
        elif tzid and isinstance(start, datetime) and start.tzinfo is not None:
            line = f"EXDATE;TZID={tzid.group(1)}:{instant.astimezone(start.tzinfo):{stamp}}"
        elif tzid:
            line = f"EXDATE;TZID={tzid.group(1)}:{wall:{stamp}}"
        else:
            line = f"EXDATE:{wall:{stamp}}"

    if wall.isoformat() in {d.strip() for d in master.exdate.split(",")}:
        return block
    at = next(
        (i for i, group in enumerate(block) if i > 0 and _head(group).startswith("BEGIN:")),
        len(block) - 1,
    )
    return [*block[:at], _fold(line), *block[at:]]
