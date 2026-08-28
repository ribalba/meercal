"""Reminders, as the web app sees them.

The server keeps its side of the bargain here as everywhere else: it reads the
queue and writes intent, and never sends a notification. Snoozing is a note the
dispatcher reads on its next tick, the same shape as ``sync_request``.

Three things live here:

* **the queue**: what is coming, what was missed, what failed. A channel that
  has been failing for a week belongs on screen, not only in the agent's stdout.
* **per-event control**: the bell in the event panel. ``GET`` answers "what
  will this event actually do", resolved through the whole precedence chain, so
  the panel can show the inherited state before anything is changed.
* **the in-app channel**: reminders addressed to ``kind = "app"`` are claimed
  by the browser from the same queue as everything else, which is what makes it
  compose with the rest instead of being a second way for a reminder to happen.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.models import Calendar, Event, EventReminder, Occurrence, ReminderDelivery
from core.reminders import (
    DISMISSED,
    INHERIT,
    OFF,
    ON,
    PENDING,
    SENT,
    channel_state,
    fire_at,
    occurrence_key,
    overrides_for,
)
from core.timeutil import display_zone, from_utc, parse_duration, utcnow
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["reminders"], dependencies=[Depends(require_auth)])
settings = get_settings()
TZ = display_zone(settings.timezone)

# What the in-app channel is allowed to pick up in one poll. A browser that has
# been shut for a week should not be handed a hundred stale popups at once.
APP_BATCH = 5


def _channel_names() -> list[str]:
    return list(settings.reminder_channels)


def _delivery_json(row: ReminderDelivery) -> dict:
    payload = row.payload or {}
    return {
        "id": row.id,
        "uid": row.uid,
        "calendar_id": row.calendar_id,
        "event_id": row.event_id,
        "channel": row.channel,
        "rule": row.rule,
        "state": row.state,
        "title": payload.get("title") or payload.get("summary") or "",
        "body": payload.get("body", ""),
        "fire_at": from_utc(row.fire_at_utc, TZ).isoformat(timespec="seconds"),
        "start": (
            row.occurrence_start_utc if row.all_day else from_utc(row.occurrence_start_utc, TZ)
        ).isoformat(timespec="seconds"),
        "all_day": row.all_day,
        "error": row.error,
        "attempts": row.attempts,
    }


@router.get("/reminders")
def list_reminders(
    state: str = Query("", description="Comma-separated states; default is everything live"),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """What is coming, and what went wrong.

    The default is the live set (pending, plus whatever failed or was missed)
    because those are the two questions the sidebar exists to answer: what am I
    about to be told, and is anything broken.
    """
    wanted = [s for s in state.split(",") if s.strip()] or ["pending", "failed", "missed", "capped"]
    rows = db.execute(
        select(ReminderDelivery)
        .where(ReminderDelivery.state.in_(wanted))
        .order_by(ReminderDelivery.fire_at_utc)
        .limit(limit)
    ).scalars().all()
    return {
        "reminders": [_delivery_json(r) for r in rows],
        "channels": [
            {"name": n, "kind": c.kind, "in_app": c.kind == "app"}
            for n, c in settings.reminder_channels.items()
        ],
        "rules": [
            {"name": r.name, "match": r.match, "channels": r.channels}
            for r in settings.reminder_rules
        ],
    }


# --- the in-app channel ----------------------------------------------------


@router.post("/reminders/claim")
def claim_for_app(db: Session = Depends(get_db)) -> dict:
    """Hand the browser the reminders addressed to it.

    Claimed with the same ``SKIP LOCKED`` idiom the agent uses, so two open
    windows do not both pop the same notification. Marked ``sent`` on handover
    rather than on display: the browser has no way to tell us it drew it, and a
    duplicate popup is worse than a missed one for this particular channel.
    """
    app_channels = [n for n, c in settings.reminder_channels.items() if c.kind == "app"]
    if not app_channels:
        return {"reminders": []}

    now = utcnow()
    grace = timedelta(seconds=settings.reminders_grace)
    rows = db.execute(
        select(ReminderDelivery)
        .where(
            ReminderDelivery.state == PENDING,
            ReminderDelivery.channel.in_(app_channels),
            ReminderDelivery.fire_at_utc <= now,
            ReminderDelivery.fire_at_utc > now - grace,
        )
        .order_by(ReminderDelivery.fire_at_utc)
        .limit(APP_BATCH)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    for row in rows:
        row.state = SENT
        row.sent_at = now
        row.claimed_by = "browser"
        row.claimed_at = now
    db.commit()
    return {"reminders": [_delivery_json(r) for r in rows]}


class SnoozeIn(BaseModel):
    minutes: int = 10


@router.post("/reminders/{reminder_id}/snooze")
def snooze(reminder_id: int, body: SnoozeIn, db: Session = Depends(get_db)) -> dict:
    row = db.get(ReminderDelivery, reminder_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such reminder")
    minutes = max(1, min(body.minutes, 24 * 60))
    row.fire_at_utc = utcnow() + timedelta(minutes=minutes)
    row.state = PENDING
    row.claimed_by, row.claimed_at, row.sent_at = "", None, None
    row.attempts, row.error = 0, ""
    db.commit()
    return _delivery_json(row)


@router.post("/reminders/{reminder_id}/dismiss")
def dismiss(reminder_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(ReminderDelivery, reminder_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such reminder")
    row.state = DISMISSED
    db.commit()
    return _delivery_json(row)


# --- per-event control -----------------------------------------------------


class EventReminderIn(BaseModel):
    # {"call": "off"}; a channel set to "inherit" is removed rather than
    # stored, because inherit is the absence of an opinion and storing it as a
    # value would make a later rule unable to reach this event.
    channels: dict[str, str] = {}
    leads: list[str] | None = None
    scope: str = "series"        # series | occurrence

    # `publish` (writing the reminder back to the calendar server as a VALARM,
    # so the phone rings too) has a column waiting for it in `event_reminders`
    # but nothing behind it yet. It is deliberately not accepted here: a
    # setting that stores cleanly and does nothing is worse than one that does
    # not exist, because it looks like it worked.


def _event_or_404(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")
    return event


@router.get("/events/{event_id}/reminders")
def event_reminders(event_id: int, db: Session = Depends(get_db)) -> dict:
    """What this event will actually do, per channel, and why.

    Resolved through the whole precedence chain rather than reporting only what
    is stored, so the panel shows the inherited state before anything is
    changed, which makes it the quickest way to find out whether a rule does
    what you meant it to.
    """
    event = _event_or_404(db, event_id)
    calendar = db.get(Calendar, event.calendar_id)
    overrides = overrides_for(db, {event.calendar_id})

    # The next instance is the one the panel is really asking about, and for a
    # non-recurring event it is the only one.
    occ = db.execute(
        select(Occurrence)
        .where(Occurrence.event_id == event.id, Occurrence.start_utc >= utcnow())
        .order_by(Occurrence.start_utc)
        .limit(1)
    ).scalar_one_or_none() or db.execute(
        select(Occurrence)
        .where(Occurrence.event_id == event.id)
        .order_by(Occurrence.start_utc.desc())
        .limit(1)
    ).scalar_one_or_none()
    rec_key = occurrence_key(occ, event) if occ is not None else event.recurrence_id

    # Which rules reach this event, and with what timing. Matching is done by
    # asking the same query the scheduler asks, restricted to this event.
    from core.query import occurrences_in_range, parse_query

    matched: dict[str, dict] = {}
    if occ is not None:
        for rule in settings.reminder_rules:
            hits = occurrences_in_range(
                db,
                occ.start_utc - timedelta(seconds=1),
                occ.start_utc + timedelta(seconds=max(event.duration_s, 1)),
                calendar_ids=[event.calendar_id],
                spec=parse_query(rule.match, rule.regex),
                include_hidden=not rule.visible_only,
            )
            if not any(h.event_id == event.id for h in hits):
                continue
            if rule.except_:
                skips = occurrences_in_range(
                    db,
                    occ.start_utc - timedelta(seconds=1),
                    occ.start_utc + timedelta(seconds=max(event.duration_s, 1)),
                    calendar_ids=[event.calendar_id],
                    spec=parse_query(rule.except_, rule.regex),
                    include_hidden=True,
                )
                if any(h.event_id == event.id for h in skips):
                    continue
            for name in rule.channels:
                matched.setdefault(name, {"rule": rule.name or rule.match, "leads": []})
                if rule.at:
                    matched[name]["leads"] = [rule.at]
                else:
                    matched[name]["leads"] = [str(x) for x in rule.leads] or ["valarm"]

    recurring = bool(event.rrule or event.rdate)
    out = []
    for name, cfg in settings.reminder_channels.items():
        state, why = channel_state(
            overrides, event.calendar_id, event.uid, rec_key, name, recurring=recurring
        )
        inherited = matched.get(name)
        out.append(
            {
                "name": name,
                "kind": cfg.kind,
                "state": state,
                # What it resolves to right now: the answer the panel shows.
                "effective": (state == ON) or (state == INHERIT and inherited is not None),
                "why": why or (f"{inherited['rule']}" if inherited else "no rule matches"),
                "leads": inherited["leads"] if inherited else [],
            }
        )

    stored_series = overrides.get((event.calendar_id, event.uid, ""))
    stored_occ = overrides.get((event.calendar_id, event.uid, rec_key)) if rec_key else None
    return {
        "event_id": event.id,
        "recurring": recurring,
        "recurrence_key": rec_key,
        "calendar": calendar.label if calendar else "",
        "channels": out,
        "alarms": event.alarms or [],
        "series": {"channels": (stored_series.channels if stored_series else {}),
                   "leads": (stored_series.leads if stored_series else None)},
        "occurrence": {"channels": (stored_occ.channels if stored_occ else {}),
                       "leads": (stored_occ.leads if stored_occ else None)},
    }


@router.put("/events/{event_id}/reminders")
def set_event_reminders(
    event_id: int, body: EventReminderIn, db: Session = Depends(get_db)
) -> dict:
    """Set this event's own opinion, for the series or for one instance."""
    event = _event_or_404(db, event_id)
    known = set(_channel_names())
    bad = [c for c in body.channels if c not in known]
    if bad:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown channel(s): {', '.join(bad)}. Configured: {', '.join(sorted(known)) or 'none'}",
        )
    if any(v not in (ON, OFF, INHERIT) for v in body.channels.values()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"State must be {ON}, {OFF} or {INHERIT}")
    for lead in (body.leads or []):
        try:
            parse_duration(lead)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    rec_key = ""
    if body.scope == "occurrence":
        occ = db.execute(
            select(Occurrence)
            .where(Occurrence.event_id == event.id, Occurrence.start_utc >= utcnow())
            .order_by(Occurrence.start_utc)
            .limit(1)
        ).scalar_one_or_none()
        if occ is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That event has no upcoming instance")
        rec_key = occurrence_key(occ, event)

    row = db.execute(
        select(EventReminder).where(
            EventReminder.calendar_id == event.calendar_id,
            EventReminder.uid == event.uid,
            EventReminder.recurrence_id == rec_key,
        )
    ).scalar_one_or_none()

    # `inherit` is stored as the absence of a key. Keeping it as a value would
    # mean a rule added next month could never reach this event again.
    channels = {k: v for k, v in body.channels.items() if v in (ON, OFF)}

    if not channels and not body.leads:
        if row is not None:
            db.delete(row)
            db.commit()
        return {"cleared": True, "scope": body.scope}

    if row is None:
        row = EventReminder(
            calendar_id=event.calendar_id, uid=event.uid, recurrence_id=rec_key
        )
        db.add(row)
    row.channels = channels
    row.leads = body.leads
    db.commit()

    # A change of mind has to reach reminders already in the queue: a mute set
    # five minutes before the call is a mute, not a preference for next time.
    muted = [name for name, state in channels.items() if state == OFF]
    if muted:
        db.execute(
            ReminderDelivery.__table__.delete().where(
                ReminderDelivery.calendar_id == event.calendar_id,
                ReminderDelivery.uid == event.uid,
                ReminderDelivery.channel.in_(muted),
                ReminderDelivery.state == PENDING,
            )
        )
        db.commit()

    return {"scope": body.scope, "recurrence_key": rec_key, "channels": row.channels}
