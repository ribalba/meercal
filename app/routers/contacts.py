"""Attendees, from the people you actually write to.

meerail already keeps a materialised address book — every from/to/cc it has
seen, with how often — and a co-recipient graph of who you address together.
That is a far better source for "who am I inviting" than an address book nobody
maintains, and it is the first place the two programs meet.

Read-only, and over a separate engine: meercal never writes to meerail's
database, and a meerail that is not installed simply means this endpoint says
so and the attendee field falls back to free text.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, Query
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from core.config import get_settings
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["contacts"], dependencies=[Depends(require_auth)])
settings = get_settings()


@lru_cache(maxsize=1)
def _engine():
    if not settings.meerail_database_url:
        return None
    # Small pool, short timeout: this is a nice-to-have on a keystroke path,
    # and it must never be the reason the composer hangs.
    return create_engine(
        settings.meerail_database_url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


_LOOKUP = text(
    """
    SELECT address, name, count
    FROM contacts
    WHERE address ILIKE :pat OR name ILIKE :pat
    ORDER BY count DESC, last_seen DESC NULLS LAST
    LIMIT :limit
    """
)


@router.get("/contacts")
def contacts(q: str = Query(""), limit: int = Query(12, le=50)) -> dict:
    engine = _engine()
    if engine is None:
        return {"configured": False, "people": []}
    if len(q.strip()) < 2:
        return {"configured": True, "people": []}
    try:
        with engine.connect() as conn:
            rows = conn.execute(_LOOKUP, {"pat": f"%{q.strip()}%", "limit": limit}).mappings().all()
    except SQLAlchemyError as exc:
        # A mail database that is down is not this program's problem to solve,
        # but it is this program's problem to survive.
        return {"configured": True, "people": [], "error": str(exc.__class__.__name__)}
    return {
        "configured": True,
        "people": [{"email": r["address"], "name": r["name"], "count": r["count"]} for r in rows],
    }
