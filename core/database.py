"""Engine, session factory and the schema bootstrap.

The agent and the web app both import this: Postgres is the only channel
between them, so it is also the only thing they share besides `core`.
"""

from __future__ import annotations

import time
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    future=True,
    # The agent holds a connection across long sync passes and the server holds
    # one per SSE stream; both outlive whatever a firewall considers idle.
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db(timeout: float = 60.0) -> None:
    """Block until Postgres answers, or give up loudly.

    Compose starts the database and the app together and `depends_on` only
    waits for the container, not for the server inside it. Retrying here is the
    difference between a first `make up` that works and one that needs a second.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:  # not up yet
            last = exc
            time.sleep(1.0)
    raise SystemExit(f"meercal: database not reachable at {settings.database_url!r}: {last}")


# Everything SQLAlchemy's create_all cannot express. Each statement is written
# to be safe to run on every start: this is a schema bootstrap, not a
# migration framework, and the project is young enough that it does not need
# one yet.
_EXTRA_DDL = (
    # Regex search over the whole calendar, the same way meerail searches mail.
    # Without the trigram index `~*` is a sequential scan over every event.
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS ix_events_search_trgm "
    "ON events USING gin (search_text gin_trgm_ops)",
    # The span rail asks one question over and over: what long things cover
    # this range? A partial index keeps that off the 99% of rows that are
    # ordinary hour-long meetings.
    "CREATE INDEX IF NOT EXISTS ix_occ_spans "
    "ON occurrences (start_utc, end_utc) WHERE span_days > 1",
    # `color_pinned` arrived after the first calendars did, and create_all only
    # ever adds whole tables. Harmless on a database that already has it.
    "ALTER TABLE calendars ADD COLUMN IF NOT EXISTS color_pinned BOOLEAN NOT NULL DEFAULT FALSE",
)


def init_db() -> None:
    from . import models  # noqa: F401; registers the mappers before create_all

    wait_for_db()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for stmt in _EXTRA_DDL:
            conn.execute(text(stmt))
