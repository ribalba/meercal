"""What the agent is doing, and asking it to do it now.

The web app cannot sync anything itself: it holds no credentials and speaks no
CalDAV. "Refresh" therefore means leaving a note the agent reads on its next
tick, which is a second or two away, not a request that blocks on a network the
server has no access to.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.models import Account, Calendar, Event, PendingAction, Setting
from core.timeutil import utcnow
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["sync"], dependencies=[Depends(require_auth)])
settings = get_settings()

# How long an account may be quiet before the UI calls it stalled: two intervals
# plus a minute of slack, so an ordinary slow pass is never reported as a fault.
STALE_AFTER = 2 * settings.agent_interval + 60

# Enough to say what went wrong in a tooltip, not a second queue view.
FAILURES_SHOWN = 10


def _unsent(db: Session) -> list[tuple[PendingAction, str]]:
    """Changes made here that are not on the server, with the event's title.

    Two kinds: one still being retried that has failed more often than not, and
    one the agent has given up on. The second used to drop out of the count at
    the very moment it became true, because ``failed`` is not ``queued``: the
    warning went away exactly when the change was abandoned, and a moved meeting
    stayed on this screen and nowhere else without a word said about it.

    A given-up change stops counting once there is nothing left to send: its
    event was deleted here (the foreign key nulls ``event_id``), or a later
    change to the same event has gone through, which carried the current state.
    A failed delete has no event to point at and is not counted.
    """
    rows = db.execute(
        select(PendingAction, Event.summary)
        .join(Event, Event.id == PendingAction.event_id, isouter=True)
        .where(
            or_(
                and_(PendingAction.state == "queued", PendingAction.attempts > 3),
                and_(PendingAction.state == "failed", PendingAction.event_id.is_not(None)),
            )
        )
        .order_by(PendingAction.id)
    ).all()
    last_done = dict(
        db.execute(
            select(PendingAction.event_id, func.max(PendingAction.id))
            .where(PendingAction.state == "done", PendingAction.event_id.is_not(None))
            .group_by(PendingAction.event_id)
        ).all()
    )
    return [
        (action, summary or "")
        for action, summary in rows
        if action.state == "queued" or last_done.get(action.event_id, 0) < action.id
    ]


@router.get("/sync/status")
def status(db: Session = Depends(get_db)) -> dict:
    accounts = db.execute(select(Account)).scalars().all()
    now = utcnow()
    queued = db.execute(
        select(PendingAction).where(PendingAction.state == "queued")
    ).scalars().all()
    unsent = _unsent(db)
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
        "failing": len(unsent),
        "failures": [
            {"id": a.id, "kind": a.kind, "state": a.state, "summary": s, "error": a.error}
            for a, s in unsent[:FAILURES_SHOWN]
        ],
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
