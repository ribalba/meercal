"""One request that tells the client everything it needs before it draws.

Deliberately a single call: the alternative is four round trips before the
first pixel, and on a cold start the calendar has nothing useful to show until
all four have landed anyway.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.models import Account, Calendar, CalendarSet, CalendarSetMember, Setting
from core.version import VERSION
from ..serialize import TZ, calendar_json
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["state"], dependencies=[Depends(require_auth)])
settings = get_settings()


@router.get("/state")
def state(db: Session = Depends(get_db)) -> dict:
    accounts = db.execute(select(Account).order_by(Account.id)).scalars().all()
    calendars = db.execute(
        select(Calendar).order_by(Calendar.position, Calendar.id)
    ).scalars().all()
    sets = db.execute(select(CalendarSet).order_by(CalendarSet.position, CalendarSet.id)).scalars().all()
    members: dict[int, list[int]] = {}
    for row in db.execute(select(CalendarSetMember)).scalars().all():
        members.setdefault(row.set_id, []).append(row.calendar_id)
    prefs = db.get(Setting, "ui")

    return {
        "version": VERSION,
        "timezone": str(TZ),
        "week_start": settings.week_start,
        "day_start": settings.day_start,
        "day_end": settings.day_end,
        "default_view": settings.default_view,
        "horizon": {
            "past_days": settings.horizon_past_days,
            "future_days": settings.horizon_future_days,
        },
        "meerail": bool(settings.meerail_database_url),
        "accounts": [
            {
                "id": a.id,
                "label": a.label,
                "kind": a.kind,
                "username": a.username,
                "active": a.active,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
                "error": a.last_error,
            }
            for a in accounts
        ],
        "calendars": [calendar_json(c) for c in calendars],
        "sets": [
            {
                "id": s.id,
                "name": s.name,
                "hotkey": s.hotkey,
                "calendars": members.get(s.id, []),
            }
            for s in sets
        ],
        "prefs": (prefs.value if prefs else {}),
    }
