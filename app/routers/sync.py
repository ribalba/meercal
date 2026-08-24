"""What the agent is doing, and asking it to do it now.

The web app cannot sync anything itself: it holds no credentials and speaks no
CalDAV. "Refresh" therefore means leaving a note the agent reads on its next
tick, which is a second or two away, not a request that blocks on a network the
server has no access to.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.models import Account, Calendar, PendingAction, Setting
from core.timeutil import utcnow
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["sync"], dependencies=[Depends(require_auth)])
settings = get_settings()

# How long an account may be quiet before the UI calls it stalled: two intervals
# plus a minute of slack, so an ordinary slow pass is never reported as a fault.
STALE_AFTER = 2 * settings.agent_interval + 60


@router.get("/sync/status")
def status(db: Session = Depends(get_db)) -> dict:
    accounts = db.execute(select(Account)).scalars().all()
    now = utcnow()
    queued = db.execute(
        select(PendingAction).where(PendingAction.state == "queued")
    ).scalars().all()
    failed = [p for p in queued if p.attempts > 3]
    return {
        "accounts": [
            {
                "id": a.id,
                "label": a.label,
                "kind": a.kind,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
                # A local calendar has no server and no agent, so it is never
                # behind, and reporting it as stalled would be a warning that can
                # never be cleared.
                "stale": bool(
                    a.active
                    and a.kind != "local"
                    and (a.last_sync_at is None or (now - a.last_sync_at).total_seconds() > STALE_AFTER)
                ),
                "error": a.last_error,
            }
            for a in accounts
        ],
        "calendars_with_errors": [
            {"id": c.id, "name": c.label, "error": c.last_error}
            for c in db.execute(select(Calendar).where(Calendar.last_error != "")).scalars().all()
        ],
        "queued": len(queued),
        "failing": len(failed),
        "interval": settings.agent_interval,
    }


@router.post("/sync/now")
def sync_now(db: Session = Depends(get_db)) -> dict:
    row = db.get(Setting, "sync_request")
    stamp = utcnow().isoformat()
    if row is None:
        db.add(Setting(key="sync_request", value={"at": stamp}))
    else:
        row.value = {"at": stamp}
    db.commit()
    return {"requested_at": stamp}
