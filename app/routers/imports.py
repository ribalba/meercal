"""An .ics file from outside, dropped on the window.

The parsing is not new: `core/cal/parse.py` already turns iCalendar into rows,
because the agent has to read whatever a server sends. What is new here is the
*asking*: a file arrives with no opinion about which calendar it belongs to,
and guessing is worse than a question. So this is two calls rather than one.
``/import/preview`` reads the file and says what is in it (how many events,
which dates, what the file calls itself) and the client shows that beside a
list of calendars; ``/import`` does the writing once a calendar has been named.

The file is uploaded twice, once per call. It is a calendar file -- kilobytes,
occasionally a megabyte -- and the alternative is server-side state keyed by a
token, with an expiry and a sweeper, to save a round trip nobody is waiting on.

Importing into a calendar that has a server behind it queues the same
``PendingAction`` an edit does, so the agent PUTs the events like any other
change. That is not a nicety: the sync pass prunes rows the server did not
send, so an event written here and never pushed would disappear at the next
pass, which is the worst of the available behaviours.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.cal.ingest import get_or_create_account, next_color, upsert_event
from core.cal.parse import ParsedEvent, calendar_name, parse_calendar
from core.config import get_settings
from core.database import get_db
from core.expand import horizon
from core.models import Account, Calendar, Event, PendingAction
from ..security import require_auth
from ..serialize import TZ, calendar_json

router = APIRouter(prefix="/api", tags=["import"], dependencies=[Depends(require_auth)])
settings = get_settings()

# A calendar year of a busy person is well under a megabyte; ten years of a
# shared team calendar is a few. Past this it is not a calendar being moved,
# and every event costs a recurrence expansion.
MAX_BYTES = 12 * 1024 * 1024
MAX_EVENTS = 5000

# Enough of the file to recognise it by. The preview is a sentence and a
# handful of titles, not a second calendar view.
SAMPLE_TITLES = 6

# The account new calendars made by an import belong to. It is a section
# heading in the sidebar, so it is worded as one.
IMPORT_ACCOUNT = "Imported"


def _decode(raw: bytes) -> str:
    """Bytes as text, forgiving about what wrote them.

    RFC 5545 says UTF-8 and most things comply, but calendars exported by
    older Windows software arrive in cp1252, and one smart quote in one event
    title is not a reason to refuse a file of four hundred.
    """
    for codec in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


async def _read(upload: UploadFile) -> str:
    """The upload as text, refusing anything past the cap while reading it.

    Chunked rather than `await upload.read()`: the point of a limit is not to
    have the whole file in memory before deciding it is too big.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(64 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    return _decode(b"".join(chunks))


def _parse(text: str, default_tz: str) -> list[ParsedEvent]:
    if "BEGIN:VCALENDAR" not in text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That is not an iCalendar file: there is no BEGIN:VCALENDAR in it",
        )
    events = parse_calendar(text, default_tz=default_tz)
    if not events:
        # A VCALENDAR carrying only VTODOs or VJOURNALs parses fine and holds
        # nothing this program draws, which is worth saying plainly rather
        # than reporting as zero events imported.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "There are no events in that file",
        )
    if len(events) > MAX_EVENTS:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"That file holds {len(events)} events; the limit is {MAX_EVENTS}",
        )
    return events


def _span(events: list[ParsedEvent]) -> tuple[datetime, datetime]:
    return min(e.dtstart for e in events), max(e.dtend for e in events)


@router.post("/import/preview")
async def preview(file: UploadFile = File(...)) -> dict:
    """What is in the file, before anyone has to say where it should go."""
    text = await _read(file)
    events = _parse(text, default_tz=str(TZ))
    first, last = _span(events)
    past, future = horizon(settings)

    return {
        "filename": file.filename or "calendar.ics",
        # X-WR-CALNAME if the file names itself; the file name otherwise. This
        # is only ever a suggestion for the "new calendar" field.
        "name": calendar_name(text) or _stem(file.filename or ""),
        "events": len(events),
        "recurring": sum(1 for e in events if e.rrule or e.rdate),
        "all_day": sum(1 for e in events if e.all_day),
        "first": first.date().isoformat(),
        "last": last.date().isoformat(),
        "titles": [e.summary for e in events[:SAMPLE_TITLES] if e.summary],
        # Events outside the rolling horizon are stored but not expanded into
        # occurrences, so they import correctly and draw nowhere. Saying so
        # here is the difference between a limitation and a bug report.
        "outside_horizon": sum(1 for e in events if e.dtend < past or e.dtstart > future),
        "horizon": {"from": past.date().isoformat(), "to": future.date().isoformat()},
    }


def _stem(filename: str) -> str:
    """`Holidays 2026.ics` as `Holidays 2026`."""
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in (".ics", ".ical", ".ifb", ".icalendar"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _slug(name: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    return "".join(keep).strip("-") or "imported"


def _new_calendar(db: Session, name: str) -> Calendar:
    """A local calendar to import into, under the Imported heading.

    Local, so the events stay here: a calendar made by dropping a file has no
    server to write to, and inventing one is not this endpoint's business.
    """
    account = get_or_create_account(db, IMPORT_ACCOUNT, "local", "", "")
    base = f"local://imported/{_slug(name)}"
    url, n = base, 2
    # Two files called "holidays.ics" are two calendars, not one calendar
    # imported twice: the URL is what makes them distinct rows.
    while db.execute(
        select(Calendar).where(Calendar.account_id == account.id, Calendar.url == url)
    ).scalar_one_or_none() is not None:
        url = f"{base}-{n}"
        n += 1
    cal = Calendar(
        account_id=account.id,
        url=url,
        name=name,
        color=next_color(db),
        tz_id=str(TZ),
        position=db.execute(select(func.count(Calendar.id))).scalar_one() or 0,
    )
    db.add(cal)
    db.flush()
    return cal


def _existing(db: Session, calendar_id: int | None) -> Calendar:
    if not calendar_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Which calendar?")
    cal = db.get(Calendar, calendar_id)
    if cal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such calendar")
    if cal.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{cal.label} is read-only")
    return cal


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_ics(
    file: UploadFile = File(...),
    calendar_id: int | None = Form(None),
    new_calendar: str = Form(""),
    db: Session = Depends(get_db),
) -> dict:
    """The file, into the calendar the user picked.

    An event already there under the same UID is updated rather than added a
    second time, which is what makes re-importing a corrected export do the
    obvious thing instead of doubling every entry.
    """
    text = await _read(file)
    name = new_calendar.strip()[:300]
    # Resolved before anything is written, and the file is parsed before the
    # calendar is made: a file that turns out not to be a calendar should not
    # leave an empty one behind under Imported.
    target = None if name else _existing(db, calendar_id)
    events = _parse(text, default_tz=(target.tz_id if target else "") or str(TZ))
    cal = target or _new_calendar(db, name)

    account = db.get(Account, cal.account_id)
    remote = account is not None and account.kind != "local"
    window = horizon(settings)

    created = updated = 0
    for parsed in events:
        existing = db.execute(
            select(Event).where(
                Event.calendar_id == cal.id,
                Event.uid == parsed.uid,
                Event.recurrence_id == parsed.recurrence_id,
            )
        ).scalar_one_or_none()
        event = upsert_event(db, cal, parsed, window)
        if existing is None:
            created += 1
        else:
            updated += 1
        if remote:
            # The agent owns the credentials, so it does the writing. Without
            # this the next full sync pass would prune every row just written.
            db.add(
                PendingAction(
                    kind="update" if event.url else "create",
                    calendar_id=cal.id,
                    event_id=event.id,
                    payload={"uid": event.uid, "url": event.url, "etag": event.etag},
                )
            )
    db.commit()

    return {
        "calendar": calendar_json(cal),
        "created": created,
        "updated": updated,
        # Nothing is on the server yet when this is non-zero, so the dialog
        # says so rather than letting the count look like a finished job.
        "queued": (created + updated) if remote else 0,
    }
