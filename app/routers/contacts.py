"""Attendees, from the people you actually write to.

meerail already keeps a materialised address book (every from/to/cc it has
seen, with how often) and a co-recipient graph of who you address together.
That is a far better source for "who am I inviting" than an address book nobody
maintains, and it is the first place the two programs meet.

Two questions, and meerail answers both. `/contacts` is the typeahead: three
letters, the people they match. `/contacts/related` is the one worth having —
given the people already on the invitation, who normally goes with them. A
standup has the same five names every week and nobody enjoys typing four of
them.

Read-only, and over a separate engine: meercal never writes to meerail's
database, and a meerail that is not installed simply means these endpoints say
so and the attendee field falls back to free text.
"""

from __future__ import annotations

import re
from functools import lru_cache

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from core.config import get_settings
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["contacts"], dependencies=[Depends(require_auth)])
settings = get_settings()

# A composer with more people than this on it needs no help finding the next
# one, and the seed list is an IN over an index: keep it short.
MAX_SEEDS = 10

# One received mail carrying both names is a coincidence and scores 1, so the
# floor drops it. One mail the *user* addressed to both scores 2 and survives:
# putting two people on a message is a deliberate act, and doing it once is
# already an answer to "who goes with whom". meerail's app/contacts.py is where
# that weighting is applied; this is the other end of the same number.
MIN_WEIGHT = 2


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


# A DSN carries meerail's database password, and driver errors are fond of
# quoting the thing they failed to connect to. Nothing that reaches a browser
# gets to carry a password out with it.
_CREDENTIALS = re.compile(r"//[^/@\s]*@")


def _reason(exc: Exception) -> str:
    """One line a human can act on, with any credential scrubbed out.

    The class name alone ("OperationalError") was what this used to return, and
    it is the same word whether meerail is switched off, on another host, or
    simply unreachable from inside this container -- which is the failure that
    actually happens, and the one the message now has to name.
    """
    text_ = _CREDENTIALS.sub("//", str(getattr(exc, "orig", None) or exc))
    line = text_.strip().splitlines()[0] if text_.strip() else exc.__class__.__name__
    return line[:200]


def _like(q: str) -> str:
    """A LIKE pattern from what somebody typed.

    `%` and `_` are wildcards to Postgres and characters to the person at the
    keyboard: a search for `_` matched the entire address book.
    """
    return f"%{q.replace('%', '').replace('_', '')}%"


_LOOKUP = text(
    """
    SELECT address, name, count
    FROM contacts
    WHERE address ILIKE :pat OR name ILIKE :pat
    ORDER BY count DESC, last_seen DESC NULLS LAST
    LIMIT :limit
    """
)

# Ranked by co-occurrence weight damped by how widely the candidate turns up on
# its own: without that, the person you mail most is suggested beside everyone.
# sqrt() rather than a plain divide, so a genuine frequent collaborator still
# outranks a one-off who happens to be obscure. This is meerail's ranking, kept
# deliberately identical -- the same three names should be offered whether the
# thing being written is a mail or a meeting.
_RELATED = text(
    """
    SELECT p.address_b AS address, c.name AS name, sum(p.weight) AS weight
    FROM contact_pairs p
    JOIN contacts c ON c.address = p.address_b
    WHERE p.address_a IN :seeds AND p.address_b NOT IN :seeds
    GROUP BY p.address_b, c.name, c.count
    HAVING sum(p.weight) >= :min_weight
    ORDER BY sum(p.weight) / sqrt(greatest(c.count, 1)) DESC,
             max(p.last_seen) DESC NULLS LAST
    LIMIT :limit
    """
).bindparams(bindparam("seeds", expanding=True))


def _people(rows) -> list[dict]:
    return [
        {"email": r["address"], "name": r["name"], "count": int(r["count"])}
        for r in rows
    ]


@router.get("/contacts")
def contacts(q: str = Query(""), limit: int = Query(12, le=50)) -> dict:
    engine = _engine()
    if engine is None:
        return {"configured": False, "people": []}
    if len(q.strip()) < 2:
        return {"configured": True, "people": []}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                _LOOKUP, {"pat": _like(q.strip()), "limit": limit}
            ).mappings().all()
    except SQLAlchemyError as exc:
        # A mail database that is down is not this program's problem to solve,
        # but it is this program's problem to survive -- and to say so, rather
        # than to look like an address book with nobody in it.
        return {"configured": True, "people": [], "error": _reason(exc)}
    return {"configured": True, "people": _people(rows)}


@router.get("/contacts/related")
def related(
    address: list[str] = Query(default=[]),
    limit: int = Query(4, ge=1, le=10),
) -> dict:
    """People usually invited alongside the ones already on the event."""
    engine = _engine()
    if engine is None:
        return {"configured": False, "people": []}

    seeds: list[str] = []
    for raw in address:
        # Lower-cased because that is how meerail stores them, and de-duplicated
        # because a field read straight off the screen can name somebody twice.
        candidate = (raw or "").strip().lower()
        if candidate and "@" in candidate and candidate not in seeds:
            seeds.append(candidate)
        if len(seeds) >= MAX_SEEDS:
            break
    if not seeds:
        return {"configured": True, "people": []}

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                _RELATED, {"seeds": seeds, "min_weight": MIN_WEIGHT, "limit": limit}
            ).mappings().all()
    except SQLAlchemyError as exc:
        return {"configured": True, "people": [], "error": _reason(exc)}
    return {
        "configured": True,
        "people": [
            {"email": r["address"], "name": r["name"], "weight": int(r["weight"])}
            for r in rows
        ],
    }
