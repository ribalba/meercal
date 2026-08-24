"""Search over every calendar at once, visible or not.

Hiding a calendar is about the drawing; it is not a statement that its contents
should become unfindable. When you are looking for "that dentist appointment"
you do not first want to remember which of twenty calendars it was on, so
search ignores visibility by default and says which calendar each hit came
from.

Ordering is by distance from now, upcoming first. A calendar search is nearly
always about something ahead, and the past is one keystroke further down.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from core.database import get_db
from core.models import Calendar, Event, Occurrence
from core.timeutil import utcnow
from ..query import parse_query
from ..serialize import TZ, occurrence_json
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_auth)])


@router.get("/search")
def search(
    q: str = Query(""),
    regex: bool = Query(False),
    limit: int = Query(60, le=500),
    db: Session = Depends(get_db),
) -> dict:
    spec = parse_query(q, regex)
    if not spec:
        return {"query": q, "hits": [], "ahead": 0, "behind": 0}

    from ..query import _flag_clause, _text_clause  # same predicates as the range query

    clauses = _text_clause(spec)
    if spec.calendars:
        from sqlalchemy import or_

        clauses.append(
            or_(*[
                or_(Calendar.name.ilike(f"%{c}%"), Calendar.display_name.ilike(f"%{c}%"))
                for c in spec.calendars
            ])
        )
    for flag in spec.flags:
        clauses.append(_flag_clause(flag))

    now = utcnow()

    def side(future: bool, cap: int):
        stmt = (
            select(Occurrence)
            .join(Event, Event.id == Occurrence.event_id)
            .join(Calendar, Calendar.id == Occurrence.calendar_id)
            .options(joinedload(Occurrence.event))
        )
        for clause in clauses:
            stmt = stmt.where(clause)
        if future:
            stmt = stmt.where(Occurrence.end_utc >= now).order_by(Occurrence.start_utc.asc())
        else:
            stmt = stmt.where(Occurrence.end_utc < now).order_by(Occurrence.start_utc.desc())
        return list(db.execute(stmt.limit(cap)).unique().scalars().all())

    # Two thirds ahead, one third behind: the split is what stops a weekly
    # meeting's twenty past instances from burying the next one.
    ahead = side(True, max(1, limit * 2 // 3))
    behind = side(False, limit - len(ahead))
    calendars = {c.id: c for c in db.execute(select(Calendar)).scalars().all()}
    hits = [occurrence_json(o, calendars.get(o.calendar_id)) for o in (*ahead, *behind)]
    for hit, occ in zip(hits, (*ahead, *behind)):
        cal = calendars.get(occ.calendar_id)
        hit["calendar"] = cal.label if cal else ""
        hit["color"] = cal.color if cal else ""
        hit["past"] = occ.end_utc < now
    return {"query": q, "tz": str(TZ), "hits": hits, "ahead": len(ahead), "behind": len(behind)}
