"""The filter language, and the one query the whole UI is built on.

meerail's search bar is a big part of why it is quick to use, and the same
idiom is worth having over a calendar: words are ANDed, a quoted phrase is
matched whole, and the filters are the nouns of the domain: which calendar,
who is on it, whether it is one of the long ones. Turning on Regex hands the
pattern to Postgres as a POSIX regular expression against the same indexed
column, so `standup|jour fixe` is a search and not a compromise.

The filter is not a separate mode. Everything the calendar draws goes through
``occurrences_in_range``, so a filter narrows the view you are already in
rather than taking you to a list of results.

This lives in ``core`` rather than in the web app because the agent needs it
too: a reminder rule is a filter string (see ``core/reminders.py``), which is
what lets "remind me about work meetings" be written in the language already in
the filter bar instead of a second, worse one invented for the purpose.
``app/query.py`` re-exports it, so the routers are unchanged.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session, joinedload

from .models import Calendar, Event, Occurrence

# `is:` values, and what each one asks of a row.
IS_FLAGS = ("allday", "timed", "span", "recurring", "busy", "free", "cancelled")


@dataclass
class QuerySpec:
    words: list[str] = field(default_factory=list)
    calendars: list[str] = field(default_factory=list)   # cal:
    people: list[str] = field(default_factory=list)      # with:
    places: list[str] = field(default_factory=list)      # in:
    flags: list[str] = field(default_factory=list)       # is:
    regex: bool = False

    def __bool__(self) -> bool:
        return bool(self.words or self.calendars or self.people or self.places or self.flags)


def parse_query(raw: str, regex: bool = False) -> QuerySpec:
    """`standup cal:work with:anna is:recurring`, in that or any other order."""
    spec = QuerySpec(regex=regex)
    if not raw or not raw.strip():
        return spec
    try:
        tokens = shlex.split(raw)
    except ValueError:  # an unbalanced quote while still typing
        tokens = raw.split()
    for token in tokens:
        low = token.lower()
        if low.startswith("cal:"):
            spec.calendars.append(token[4:])
        elif low.startswith("with:"):
            spec.people.append(token[5:])
        elif low.startswith("in:"):
            spec.places.append(token[3:])
        elif low.startswith("is:"):
            value = low[3:]
            if value in IS_FLAGS:
                spec.flags.append(value)
        elif low.startswith("rx:"):
            spec.regex = True
            spec.words.append(token[3:])
        else:
            spec.words.append(token)
    return spec


def _text_clause(spec: QuerySpec):
    clauses = []
    for word in spec.words:
        if not word:
            continue
        if spec.regex:
            # `~*` is Postgres' case-insensitive POSIX match. The GIN trigram
            # index can serve it whenever the pattern contains a literal run,
            # which nearly every useful pattern does.
            clauses.append(Event.search_text.op("~*")(word))
        else:
            clauses.append(Event.search_text.ilike(f"%{word}%"))
    for person in spec.people:
        clauses.append(
            or_(
                Event.search_text.ilike(f"%{person}%"),
                Event.organizer.ilike(f"%{person}%"),
            )
        )
    for place in spec.places:
        clauses.append(Event.location.ilike(f"%{place}%"))
    return clauses


def _flag_clause(flag: str):
    return {
        "allday": Occurrence.all_day.is_(True),
        "timed": Occurrence.all_day.is_(False),
        # "long" is the interesting shape in a real calendar, and it is exactly
        # what the span rail draws.
        "span": Occurrence.span_days > 1,
        "recurring": or_(Event.rrule != "", Event.rdate != ""),
        "busy": Event.transparent.is_(False),
        "free": Event.transparent.is_(True),
        "cancelled": Event.status == "CANCELLED",
    }[flag]


def occurrences_in_range(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    calendar_ids: list[int] | None = None,
    spec: QuerySpec | None = None,
    include_hidden: bool = False,
    limit: int = 20000,
) -> list[Occurrence]:
    """Everything that *overlaps* [start, end).

    Overlap, not containment: an event that started three weeks ago and ends
    next month belongs on today's screen, and asking for rows whose start falls
    inside the range is the single most common way a calendar loses them.
    """
    stmt: Select = (
        select(Occurrence)
        .join(Event, Event.id == Occurrence.event_id)
        .join(Calendar, Calendar.id == Occurrence.calendar_id)
        .options(joinedload(Occurrence.event))
        .where(Occurrence.start_utc < end, Occurrence.end_utc > start)
    )
    if calendar_ids:
        stmt = stmt.where(Occurrence.calendar_id.in_(calendar_ids))
    elif not include_hidden:
        stmt = stmt.where(Calendar.visible.is_(True))

    spec = spec or QuerySpec()
    clauses = _text_clause(spec)
    if spec.calendars:
        clauses.append(
            or_(*[
                or_(Calendar.name.ilike(f"%{c}%"), Calendar.display_name.ilike(f"%{c}%"))
                for c in spec.calendars
            ])
        )
    for flag in spec.flags:
        clauses.append(_flag_clause(flag))
    if clauses:
        stmt = stmt.where(and_(*clauses))

    # Longest first, then by start. The order is what the span rail packs lanes
    # in, and packing the long ones first is what keeps a three-week bar in the
    # same lane for its whole length instead of stepping sideways.
    stmt = stmt.order_by(Occurrence.span_days.desc(), Occurrence.start_utc.asc()).limit(limit)
    return list(db.execute(stmt).unique().scalars().all())
