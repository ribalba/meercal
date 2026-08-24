"""SQLAlchemy ORM models for meercal.

Design notes
------------
* An **event** is what the calendar server sent: one VEVENT, recurrence rule and
  all. An **occurrence** is one appearance of it in time. Recurrence is expanded
  into ``occurrences`` rows over a rolling horizon (see ``core/expand.py``) so
  that drawing a range is a single index scan over one table. The alternative,
  expanding N calendars' rules per repaint, is what makes other clients slow
  once you have twenty of them.
* Times are stored as **naive UTC**, the way meerail stores mail dates. The
  event also keeps its *local* wall time and its zone (``dtstart_local`` /
  ``tz_id``), because "09:00 every Monday" is a statement about wall clocks:
  expanding in UTC would walk the meeting an hour across a DST boundary.
* **All-day events are dates, not instants.** They are stored at midnight with
  ``all_day`` set, and nothing (not the agent, not the API, not the client)
  ever converts them between zones. A birthday is on that day in Auckland too.
* ``events.search_text`` (summary + description + location + attendees) carries
  a GIN pg_trgm index so POSIX regex (``~*``) can use an index, which is what
  makes searching every calendar at once cost what searching one does.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    # Naive UTC everywhere internally; tz-aware input is normalized at the edges.
    return datetime.now(timezone.utc).replace(tzinfo=None)


# The palette new calendars are coloured from, in order. Chosen so that any two
# neighbours stay apart for the common colour-vision deficiencies. With twenty
# calendars on screen the colour is doing real work, and a calendar you cannot
# tell from the one beside it is a calendar you have to click to read.
CALENDAR_COLORS = (
    "#1d6ff2",  # blue
    "#eb6834",  # orange
    "#2a9d5c",  # green
    "#a855f7",  # violet
    "#d70015",  # red
    "#0891b2",  # teal
    "#b45309",  # amber
    "#db2777",  # pink
    "#4f46e5",  # indigo
    "#65a30d",  # olive
)


# --- Sources ---------------------------------------------------------------


class Account(Base):
    """One calendar account, served by an agent that owns its credentials.

    The password lives in the agent's ``meercal.toml`` and never reaches this
    row or the container the web app runs in; this is identity, display and
    sync status. The agent references an account by ``label``.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="caldav", nullable=False)
    # Where the agent ended up talking to, after discovery. iCloud redirects
    # every account to a personal host, so this is worth keeping: it is the
    # first thing you want to see when one account stops syncing.
    url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    username: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    calendars: Mapped[list["Calendar"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class Calendar(Base):
    """One calendar within an account: the thing with a tickbox next to it."""

    __tablename__ = "calendars"
    __table_args__ = (UniqueConstraint("account_id", "url", name="uq_calendar_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    # What the user renamed it to. Separate from `name` so that a server-side
    # rename does not silently undo it, and clearing it here restores theirs.
    display_name: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    color: Mapped[str] = mapped_column(String(32), default=CALENDAR_COLORS[0], nullable=False)
    tz_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Whether it is drawn right now. A hidden calendar is still synced and still
    # searched; hiding is about the drawing, not about the data.
    visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # CalDAV's incremental handle (RFC 6578 sync-token, or the collection's
    # ctag). With it a quiet pass over one calendar is a single request.
    sync_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ctag: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    account: Mapped[Account] = relationship(back_populates="calendars")

    @property
    def label(self) -> str:
        return self.display_name or self.name


class CalendarSet(Base):
    """A named group of calendars, switched as one.

    The answer to "I have a lot of calendars": you do not think in calendars,
    you think in *situations*: work, family, the thing you are on call for.
    A set is those, and it has a number key so switching costs one keystroke.
    """

    __tablename__ = "calendar_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    # 1-9, the key that selects it. Null means "no key, click it".
    hotkey: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CalendarSetMember(Base):
    __tablename__ = "calendar_set_members"
    __table_args__ = (UniqueConstraint("set_id", "calendar_id", name="uq_set_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_id: Mapped[int] = mapped_column(
        ForeignKey("calendar_sets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), index=True, nullable=False
    )


# --- Events ----------------------------------------------------------------


class Event(Base):
    """One VEVENT as the server holds it: the rule, not the instances.

    A recurring series is one row. An instance the user moved or renamed comes
    back from the server as a second VEVENT with the same UID and a
    RECURRENCE-ID, and is stored as its own row here, which is why the
    uniqueness is over the triple and not over the UID.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "uid", "recurrence_id", name="uq_event_uid"),
        Index("ix_events_calendar_start", "calendar_id", "dtstart"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), index=True, nullable=False
    )
    uid: Mapped[str] = mapped_column(String(512), nullable=False)
    # The instance this row overrides, as an ISO string, or "" for the series
    # itself. A string rather than a timestamp because an all-day override is a
    # date and a timed one is an instant, and the value is only ever matched.
    recurrence_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    etag: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    location: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # TRANSP: does this block the time, or is it only a note in it? Half the
    # multi-week events in a real calendar are the second kind (a conference,
    # a release window, someone's holiday) and the Ribbon leans on it.
    transparent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The instant, for sorting and for the horizon queries.
    dtstart: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    dtend: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The same start as a wall clock in `tz_id`: what the rule is written
    # against. Expanding "every Monday at 09:00" in UTC moves it an hour twice
    # a year; expanding it here and converting each instance does not.
    dtstart_local: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    tz_id: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    # Length rather than an end: RFC 5545 gives every instance of a series the
    # master's duration, and a DST boundary inside an instance would otherwise
    # make its end wrong.
    duration_s: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    rrule: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rdate: Mapped[str] = mapped_column(Text, default="", nullable=False)
    exdate: Mapped[str] = mapped_column(Text, default="", nullable=False)

    organizer: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    # [{"email": ..., "name": ..., "status": "ACCEPTED", "role": ...}, ...]
    attendees: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    categories: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # The VALARMs the server sent, as [{"trigger": "-PT15M", "related": "START",
    # "action": "DISPLAY", "description": ...}]. Kept because a reminder rule
    # can defer to them (`lead = "valarm"`), which is what lets meercal agree
    # with the alarm your phone already set instead of arguing with it.
    alarms: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # summary + description + location + attendee names and addresses, which is
    # what a regex search runs over.
    search_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # The VEVENT as received. Round-tripping an edit through a parse and a
    # re-serialise loses the properties this program does not model yet; the
    # agent patches this text instead.
    raw_ics: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    occurrences: Mapped[list["Occurrence"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", passive_deletes=True
    )


class Occurrence(Base):
    """One appearance of an event in time, materialised.

    Rebuilt from the event whenever the event changes or the horizon rolls.
    ``calendar_id`` is denormalised from the event on purpose: every query the
    UI makes filters by calendar *and* by range, and a join to reach the filter
    would give up the index that makes the range cheap.
    """

    __tablename__ = "occurrences"
    __table_args__ = (
        Index("ix_occ_range", "start_utc", "end_utc"),
        Index("ix_occ_calendar_start", "calendar_id", "start_utc"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), index=True, nullable=False
    )
    start_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # How many days this instance touches. Written here rather than derived so
    # that "give me the long things over this range" (the query behind the
    # span rail) is a partial index scan and not a computation over every row.
    span_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    event: Mapped[Event] = relationship(back_populates="occurrences")


# --- Local state -----------------------------------------------------------


class Setting(Base):
    """UI and server state that outlives a browser: chosen view, visible
    calendars, the last set. Key/value so that adding one is not a migration."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class PendingAction(Base):
    """Something the user did that has to reach a calendar server.

    The web app never speaks CalDAV: it writes the change to the database and
    a row here, and the agent drains the queue. The UI shows the change
    immediately, which is the only way an edit feels instant on a server three
    hundred milliseconds away, and the row is what makes it true afterwards.
    """

    __tablename__ = "pending_actions"
    __table_args__ = (Index("ix_pending_state", "state", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # create|update|delete|rsvp
    calendar_id: Mapped[int | None] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# --- Reminders -------------------------------------------------------------
#
# Two tables and one rule: nothing durable may point at an ``occurrences`` row.
# ``core/expand.py`` rebuilds occurrences by deleting and re-inserting them,
# and ``roll_horizon`` does that for every event in the database once a day, so
# an id here would be dangling within hours and a foreign key would take the
# reminder with it. Both tables therefore carry the *natural* key: the
# calendar, the UID, and (for a delivery) the instant the instance starts.
#
# That key also has the right behaviour when an event moves: the start changes,
# so the key changes, so a fresh reminder is armed and the stale pending row is
# swept. A meeting moved to tomorrow re-arms itself.


class ReminderDelivery(Base):
    """One notification, on one channel, for one instance of one event.

    Shaped like :class:`PendingAction`, which is the same problem: something
    that has to happen outside this process, that must survive a restart, and
    whose failure has to be visible in the UI rather than only in a log.

    States::

        pending -> claimed -> sent
                           -> failed     (retried until attempts run out)
                -> missed              (fire_at fell outside the grace window)
                -> snoozed -> pending  (fire_at moved, armed again)
                -> dismissed           (the user said no before it fired)
    """

    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        # The whole idempotence story. Arming re-derives every reminder in the
        # window on every pass and relies on this to make that free.
        #
        # Note what is *not* in the key: the rule. Two rules that both say "ten
        # minutes before, on the desktop" mean one notification, not two. The
        # moment is the identity, and `rule` records which rule got there first.
        # Keying on the rule as well is the version of this that pops two
        # identical notifications the day you add a second rule.
        UniqueConstraint(
            "calendar_id", "uid", "occurrence_start_utc", "channel", "fire_at_utc",
            name="uq_reminder_delivery",
        ),
        Index("ix_reminder_due", "state", "fire_at_utc"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), index=True, nullable=False
    )
    uid: Mapped[str] = mapped_column(String(512), nullable=False)
    occurrence_start_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Names out of the configuration, not ids. Renaming a rule arms a new
    # reminder rather than silently inheriting the old one's history, which is
    # the honest reading of "this is a different rule now".
    rule: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    channel: Mapped[str] = mapped_column(String(120), nullable=False)

    fire_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    # Rendered when the reminder is armed, not when it is sent. An event
    # renamed in the hour before it starts should not rewrite what you were
    # told about it, and a channel that retries must say the same thing twice.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Which dispatcher took it. Two machines sharing one database is the normal
    # case for this program: the desktop notification has to fire on the
    # machine you are sitting at, and the phone call does not care.
    claimed_by: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Convenience for joins only: never load-bearing, and deliberately
    # SET NULL: losing the event must not lose the record that you were told.
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class EventReminder(Base):
    """What you decided about one event, overriding the rules.

    The lunch problem: a daily "Lunch" matches ``cal:work is:busy`` as squarely
    as a client meeting does, and no filter string can see the difference. So
    the event carries the last word and a rule is only the default it falls
    back to.

    ``channels`` is a *map*, not a list, because absence has to mean something.
    A channel not named here inherits whatever the rules say; a channel named
    with ``"off"`` is muted for good, including against rules that did not
    exist when the mute was set. Storing a list of the channels that should
    fire would make a mute indistinguishable from a coincidence: the copy goes
    stale the moment a rule changes, and nobody can tell afterwards whether the
    silence was meant.

    This cannot live on the ``events`` row: that row is overwritten from the
    server every time its resource syncs, so a mute would last until the next
    time somebody else edited the meeting.
    """

    __tablename__ = "event_reminders"
    __table_args__ = (
        UniqueConstraint("calendar_id", "uid", "recurrence_id", name="uq_event_reminder"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), index=True, nullable=False
    )
    uid: Mapped[str] = mapped_column(String(512), nullable=False)
    # "" is the whole series; a recurrence key is one instance of it. The two
    # levels of the precedence chain, in one table, keyed the way `events`
    # already keys its own overrides.
    recurrence_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # {"call": "off", "phone": "on"}; anything absent inherits.
    channels: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # ["1h", "10m"], or null to keep the matching rule's timing and change only
    # which channels fire. Null is the common case by a distance.
    leads: Mapped[list | None] = mapped_column(JSONB)
    # Write it back to the calendar server as a VALARM as well, so the phone
    # rings too. The column is here because it belongs to this row and not
    # somewhere else; nothing writes VALARMs yet, and the API does not accept
    # it. Off by default when it does land: on by default would mean every
    # device you own alarms twice for the same meeting.

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
