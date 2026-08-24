"""Which reminders exist, when they fire, and who gets the last word.

This is the policy half. It computes, but does not deliver: given the database
and the configuration it returns a list of :class:`Plan`, one per (instance,
rule, channel), which the agent arms into ``reminder_deliveries`` and the
dispatcher later sends. Keeping it here rather than in ``agent/`` is what lets
the web app show what *would* happen for an event without being able to make
it happen.

Two ideas carry the whole file.

**A rule is a filter string.** ``occurrences_in_range`` already answers "which
occurrences match ``cal:work is:busy`` in this window" against a trigram index,
so a rule is that question plus a lead time. No second query language.

**The event has the last word.** A daily "Lunch" matches ``cal:work is:busy``
as squarely as a client meeting does, and no filter string can see the
difference. The difference is that you know what lunch is. So an
``EventReminder`` row overrides the rules, per channel, in three states:
inherit, on, off. Inherit is not the same as on, which is the point: a channel
switched off stays off against rules that did not exist when you switched it.

The precedence chain, resolved by :func:`channel_state`::

    1  this occurrence   event_reminders (calendar, uid, recurrence key)
    2  the series        event_reminders (calendar, uid, "")
    3  the event's alarm events.alarms, if a rule asked for `lead = "valarm"`
    4  matching rules    [[reminders.rule]]
    5  nothing fires

Nothing here holds an ``occurrences.id``. ``core/expand.py`` rebuilds those
rows by deleting and re-inserting them and ``roll_horizon`` does it for every
event daily, so identity is the natural key: the calendar, the UID, and the
instant the instance starts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .expand import recurrence_key
from .models import Calendar, Event, EventReminder, Occurrence, ReminderDelivery
from .query import occurrences_in_range, parse_query
from .timeutil import (
    display_zone,
    from_utc,
    humanize_lead,
    parse_at,
    parse_clock,
    parse_duration,
    to_utc,
    utcnow,
    zone,
)

# How far past a rule's own longest lead the scheduler looks. Derived rather
# than configured: a horizon setting that quietly has to exceed a lead setting
# is a bug waiting to be filed.
ARM_MARGIN = timedelta(minutes=5)

# A channel's three states on an event. `inherit` is the absence of an opinion
# and is stored as the absence of a key, so that adding a rule later reaches
# the events that never had one.
ON, OFF, INHERIT = "on", "off", "inherit"


@dataclass
class Plan:
    """One reminder that would fire, or one that was silenced and why."""

    calendar_id: int
    uid: str
    occurrence_start_utc: datetime
    all_day: bool
    rule: str
    channel: str
    fire_at_utc: datetime
    payload: dict = field(default_factory=dict)
    event_id: int | None = None
    # "" means it will fire. Otherwise the level that silenced it, in words,
    # for `--next` to print. A reminder that does not happen must not look
    # like one nobody configured.
    muted_by: str = ""

    @property
    def key(self) -> tuple:
        return (self.calendar_id, self.uid, self.occurrence_start_utc, self.rule, self.channel)


# --- the precedence chain --------------------------------------------------


def occurrence_key(occ: Occurrence, event: Event) -> str:
    """This instance's recurrence key, spelled the way ``events`` spells it.

    An all-day occurrence is stored as its own date and is never converted; a
    timed one is stored as an instant and has to go back through the event's
    own zone to recover the wall time the rule was written against.
    """
    wall = occ.start_utc if occ.all_day else from_utc(occ.start_utc, zone(event.tz_id))
    return recurrence_key(wall, occ.all_day)


def overrides_for(db: Session, calendar_ids: set[int] | None = None) -> dict[tuple, EventReminder]:
    """Every per-event opinion, indexed by ``(calendar_id, uid, recurrence_id)``.

    Loaded in one query and held for the whole pass. There is one row per event
    somebody had an opinion about, which is a small number by construction. A
    per-occurrence lookup would be thousands of round trips to answer "no" with.
    """
    stmt = select(EventReminder)
    if calendar_ids is not None:
        if not calendar_ids:
            return {}
        stmt = stmt.where(EventReminder.calendar_id.in_(calendar_ids))
    rows = db.execute(stmt).scalars().all()
    return {(r.calendar_id, r.uid, r.recurrence_id): r for r in rows}


def channel_state(
    overrides: dict[tuple, EventReminder],
    calendar_id: int,
    uid: str,
    rec_key: str,
    channel: str,
    recurring: bool = True,
) -> tuple[str, str]:
    """Resolve one channel for one instance. Returns ``(state, why)``.

    ``why`` is empty unless something explicit answered, so the caller can say
    *muted on the whole series* rather than only *not firing*. It is named for
    the reader rather than for the schema: on an event that does not recur,
    both levels are the same thing and calling one of them "the series" would
    be an invitation to go looking for a series that is not there.
    """
    for key, level in (
        ((calendar_id, uid, rec_key), "this occurrence" if recurring else "this event"),
        ((calendar_id, uid, ""), "the whole series" if recurring else "this event"),
    ):
        row = overrides.get(key)
        if row is None:
            continue
        state = (row.channels or {}).get(channel)
        if state in (ON, OFF):
            return state, f"{'muted' if state == OFF else 'forced on'} on {level}"
    return INHERIT, ""


def leads_for(rule, event: Event) -> list[timedelta]:
    """The lead times this rule wants for this event.

    ``lead = "valarm"`` defers to whatever the calendar server sent, which is
    what lets meercal agree with the alarm your phone already set rather than
    argue with it. An event carrying no VALARM under such a rule gets nothing,
    deliberately: the rule said "whatever the server said", and the server said
    nothing.
    """
    out: list[timedelta] = []
    for item in rule.leads:
        if item == "valarm":
            out.extend(_valarm_leads(event))
            continue
        # A lead is a distance *before* the event however it is spelled, so a
        # bare "10m" and iCalendar's "-PT10M" mean the same thing here.
        out.append(abs(parse_duration(item)))
    # Two rules asking for the same moment is one reminder, not two.
    return sorted(set(out))


def _valarm_leads(event: Event) -> list[timedelta]:
    out = []
    for alarm in (event.alarms or []):
        trigger = (alarm or {}).get("trigger", "")
        if not trigger or (alarm or {}).get("related", "START").upper() != "START":
            # RELATED=END alarms are rare and mean something different; leaving
            # them out is better than firing them at the wrong end.
            continue
        try:
            delta = parse_duration(trigger)
        except ValueError:
            continue
        if delta.total_seconds() <= 0:
            out.append(abs(delta))
    return out


# --- when it fires ---------------------------------------------------------


def fire_at(occ: Occurrence, lead: timedelta, settings, tz) -> datetime:
    """When a reminder ``lead`` before this instance goes off, in naive UTC.

    All-day events are the whole reason this is not one subtraction. They are
    dates: there is no clock to lead away from, and treating the stored
    midnight as an instant would put "the day before" at 23:50 the night before
    that, in whichever zone happened to be in play. So an all-day instance is
    first given the notional wall-clock time in ``reminders.all_day_at``, in the
    display zone, and the lead is measured from there.
    """
    if not occ.all_day:
        return occ.start_utc - lead
    hour, minute = parse_clock(settings.reminders_all_day_at)
    anchor_wall = datetime.combine(occ.start_utc.date(), datetime.min.time()).replace(
        hour=hour, minute=minute
    )
    return to_utc(anchor_wall, str(tz)) - lead


def fire_at_anchor(occ: Occurrence, at: str, settings, tz) -> datetime:
    """When an absolute anchor (``-1d 18:00``) falls, in naive UTC.

    The wall clock is the point: "the evening before" is a statement about a
    clock on a wall, not a distance from an instant, and it must land at 18:00
    on both sides of a DST change.
    """
    day_offset, hour, minute = parse_at(at)
    base_date = (occ.start_utc if occ.all_day else from_utc(occ.start_utc, tz)).date()
    wall = datetime.combine(base_date + timedelta(days=day_offset), datetime.min.time()).replace(
        hour=hour, minute=minute
    )
    return to_utc(wall, str(tz))


# --- what a notification says ---------------------------------------------


class _Blanks(dict):
    """Missing keys render as themselves rather than raising.

    A typo in a `say` template must not take out the dispatcher at 06:50 and
    with it every other reminder due that morning. You get `{summry}` in the
    notification, which says exactly where to look.
    """

    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, fields: dict) -> str:
    try:
        return template.format_map(_Blanks(fields))
    except (ValueError, IndexError, AttributeError):
        # An unbalanced brace, rather than an unknown name.
        return template


def fields_for(occ: Occurrence, event: Event, calendar: Calendar, channel, tz) -> dict:
    """What a template may say, already trimmed to the channel's `detail`.

    Redaction happens here rather than at the point of sending, because it has
    to apply to every template a channel has, and because the payload is
    stored, and a payload is a copy of your calendar sitting in a table.
    """
    start = occ.start_utc if occ.all_day else from_utc(occ.start_utc, tz)
    lead = occ.start_utc - utcnow()
    when = "today" if occ.all_day else humanize_lead(lead)
    if occ.all_day:
        when = f"all day {start.strftime('%-d %B')}"

    detail = getattr(channel, "detail", "full")
    if detail == "none":
        return {
            "summary": "a reminder", "when": when, "location": "", "calendar": "",
            "calendar_suffix": "", "location_suffix": "", "start": "", "description": "",
        }

    summary = event.summary or "(no title)"
    location = event.location if detail == "full" else ""
    cal_name = calendar.label if detail == "full" else ""
    return {
        "summary": summary,
        "when": when,
        "start": start.strftime("%H:%M" if not occ.all_day else "%Y-%m-%d"),
        "location": location,
        "calendar": cal_name,
        # Suffix forms so a default body reads correctly when a field is
        # empty: "in 10 minutes · Familie · Ritterstr. 12" and "in 10 minutes"
        # from the same template.
        "calendar_suffix": f" · {cal_name}" if cal_name else "",
        "location_suffix": f" · {location}" if location else "",
        "description": (event.description or "")[:200] if detail == "full" else "",
    }


# --- planning --------------------------------------------------------------


def _rule_reach(rule) -> timedelta:
    """How far ahead of a reminder its event has to be visible.

    Derived from the rule's own longest lead rather than configured. A separate
    horizon setting that quietly has to be larger than a lead setting is a bug
    waiting to be filed: someone writes ``lead = "1w"``, nothing fires, and
    nothing says why.
    """
    reach = timedelta(0)
    for item in rule.leads:
        if item == "valarm":
            # Not knowable without reading the events; a week covers every
            # VALARM anyone actually sets.
            reach = max(reach, timedelta(days=7))
        else:
            reach = max(reach, abs(parse_duration(item)))
    if rule.at:
        day_offset, _, _ = parse_at(rule.at)
        reach = max(reach, timedelta(days=abs(day_offset) + 1))
    return reach + ARM_MARGIN


def plan(db: Session, settings, now: datetime | None = None, ahead: timedelta | None = None) -> list[Plan]:
    """Every reminder due between now and ``ahead``, including the muted ones.

    Muted plans are returned with ``muted_by`` set rather than dropped, because
    ``--next`` has to be able to answer "why didn't it ring": a reminder that
    does not happen must not be indistinguishable from one nobody configured.
    Only unmuted plans are ever armed.
    """
    now = now or utcnow()
    tz = display_zone(settings.timezone)
    grace = timedelta(seconds=settings.reminders_grace)
    window_end = now + (ahead if ahead is not None else timedelta(0))

    channels = settings.reminder_channels
    plans: dict[tuple, Plan] = {}
    # Both of these are small by construction (one row per calendar, and one
    # row per event somebody had an opinion about) so they are read once for
    # the whole pass rather than per rule or, worse, per occurrence.
    overrides = overrides_for(db)
    calendars = {c.id: c for c in db.execute(select(Calendar)).scalars()}

    for rule in settings.reminder_rules:
        reach = _rule_reach(rule)
        rows = occurrences_in_range(
            db,
            now,
            window_end + reach,
            spec=parse_query(rule.match, rule.regex),
            include_hidden=not rule.visible_only,
        )
        if not rows:
            continue

        # `except` is a second query subtracted from the first. The filter
        # language has no negation operator and does not need one for this.
        excluded: set[tuple] = set()
        if rule.except_:
            excluded = {
                (o.calendar_id, o.event.uid, o.start_utc)
                for o in occurrences_in_range(
                    db,
                    now,
                    window_end + reach,
                    spec=parse_query(rule.except_, rule.regex),
                    include_hidden=True,
                )
            }

        for occ in rows:
            if (occ.calendar_id, occ.event.uid, occ.start_utc) in excluded:
                continue
            event = occ.event
            rec_key = occurrence_key(occ, event)
            calendar = calendars.get(occ.calendar_id)
            if calendar is None:
                continue

            # An event the user cancelled is not something to be reminded of.
            if event.status == "CANCELLED":
                continue

            override = overrides.get((occ.calendar_id, event.uid, rec_key)) or overrides.get(
                (occ.calendar_id, event.uid, "")
            )
            if override is not None and override.leads:
                moments = [abs(parse_duration(x)) for x in override.leads]
            elif rule.at:
                moments = None  # anchored, not led
            else:
                moments = leads_for(rule, event)

            fires: list[datetime] = (
                [fire_at_anchor(occ, rule.at, settings, tz)]
                if moments is None
                else [fire_at(occ, lead, settings, tz) for lead in moments]
            )

            for channel_name in rule.channels:
                channel = channels.get(channel_name)
                if channel is None or not channel.enabled:
                    continue
                state, why = channel_state(
                    overrides, occ.calendar_id, event.uid, rec_key, channel_name,
                    recurring=bool(event.rrule or event.rdate),
                )
                for moment in fires:
                    if not (now - grace <= moment <= window_end):
                        continue
                    p = Plan(
                        calendar_id=occ.calendar_id,
                        uid=event.uid,
                        occurrence_start_utc=occ.start_utc,
                        all_day=occ.all_day,
                        rule=rule.name or rule.match or "(unnamed)",
                        channel=channel_name,
                        fire_at_utc=moment,
                        event_id=event.id,
                        muted_by=why if state == OFF else "",
                        payload=_payload(occ, event, calendar, channel, tz, moment),
                    )
                    # Deduped on the moment rather than on the rule: two rules
                    # both asking for ten minutes before is one notification.
                    dedupe = (p.calendar_id, p.uid, p.occurrence_start_utc, p.channel, p.fire_at_utc)
                    if dedupe not in plans or (plans[dedupe].muted_by and not p.muted_by):
                        plans[dedupe] = p

    return sorted(plans.values(), key=lambda p: (p.fire_at_utc, p.channel))


def _payload(occ, event, calendar, channel, tz, moment: datetime) -> dict:
    fields = fields_for(occ, event, calendar, channel, tz)
    # `when` is rendered against the reminder's own moment, not against now:
    # a reminder armed an hour early must still say "in 10 minutes" when it
    # arrives, and a retry must say the same thing the first attempt did.
    fields["when"] = (
        fields["when"] if occ.all_day else humanize_lead(occ.start_utc - moment)
    )
    return {
        "title": render(channel.title, fields),
        "body": render(channel.body, fields),
        "fields": fields,
        "summary": fields["summary"],
    }


# --- the queue -------------------------------------------------------------
#
# Everything below is ordinary Postgres queue work, and deliberately so. The
# hard part of scheduling a calendar is already solved upstream: occurrences
# are materialised, so "what fires in the next hour" is one indexed range
# query. A scheduler library would bring its own persistence, its own notion of
# identity and a second answer to a question the database is already answering.

PENDING, CLAIMED, SENT = "pending", "claimed", "sent"
FAILED, MISSED, DISMISSED, SNOOZED, CAPPED = "failed", "missed", "dismissed", "snoozed", "capped"
TERMINAL = (SENT, MISSED, DISMISSED, CAPPED)

# A channel that has failed this often is not about to start working. The row
# stays with its error so the UI can say so, rather than being retried forever.
MAX_ATTEMPTS = 4


def arm(db: Session, plans: list[Plan]) -> int:
    """Write the plans that are not muted into the queue. Returns how many were new.

    ``ON CONFLICT DO NOTHING`` is what makes the scheduler idempotent: it
    re-derives every reminder in the window on every pass and lets the unique
    key throw away the ones it has already armed. A crash, a restart or a
    second agent therefore changes nothing.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = [
        {
            "calendar_id": p.calendar_id,
            "uid": p.uid,
            "occurrence_start_utc": p.occurrence_start_utc,
            "all_day": p.all_day,
            "rule": p.rule[:120],
            "channel": p.channel,
            "fire_at_utc": p.fire_at_utc,
            "state": PENDING,
            "payload": p.payload,
            "event_id": p.event_id,
        }
        for p in plans
        if not p.muted_by
    ]
    if not rows:
        return 0
    # RETURNING rather than rowcount: with ON CONFLICT DO NOTHING the driver
    # reports -1 as often as it reports a number, and "how many were new" is
    # the one thing a scheduler pass has to say out loud.
    stmt = (
        pg_insert(ReminderDelivery)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["calendar_id", "uid", "occurrence_start_utc", "channel", "fire_at_utc"]
        )
        .returning(ReminderDelivery.id)
    )
    return len(db.execute(stmt).scalars().all())


def discard_stale(db: Session) -> int:
    """Drop pending reminders whose instance no longer exists.

    An event that moved changes its start, which changes the key, which arms a
    fresh reminder, so the old one has to go or you are told about a meeting
    at the time it used to be at. Only ``pending`` rows: a reminder already
    sent is a record of something that happened and is not ours to rewrite.

    Deleting on "no matching occurrence" is safe against the constant
    delete-then-insert in ``expand.py`` because that happens inside one
    transaction: another session sees the old set or the new one, never neither.
    """
    from sqlalchemy import text

    result = db.execute(
        text(
            """
            DELETE FROM reminder_deliveries d
             WHERE d.state = :pending
               AND NOT EXISTS (
                     SELECT 1
                       FROM occurrences o
                       JOIN events e ON e.id = o.event_id
                      WHERE o.calendar_id = d.calendar_id
                        AND e.uid = d.uid
                        AND o.start_utc = d.occurrence_start_utc
                   )
            """
        ),
        {"pending": PENDING},
    )
    return result.rowcount or 0


def mark_missed(db: Session, settings, now: datetime | None = None) -> int:
    """Retire reminders that are now too late to be worth sending.

    They become ``missed`` rather than vanishing. A laptop that was shut for
    two days should be able to say which reminders it slept through. Silence
    that cannot be distinguished from "nothing was due" is what makes people
    stop trusting the feature.
    """
    from sqlalchemy import update

    now = now or utcnow()
    # The most generous grace any channel has: a per-channel one shorter than
    # this is applied at send time, where the channel is known.
    graces = [settings.reminders_grace] + [
        c.grace or settings.reminders_grace for c in settings.reminder_channels.values()
    ]
    cutoff = now - timedelta(seconds=max(graces))
    result = db.execute(
        update(ReminderDelivery)
        .where(
            ReminderDelivery.state.in_((PENDING, CLAIMED)),
            ReminderDelivery.fire_at_utc < cutoff,
        )
        .values(state=MISSED, error="not delivered within the grace window")
    )
    return result.rowcount or 0


def purge(db: Session, settings, now: datetime | None = None) -> int:
    """Forget finished reminders after ``retention_days``."""
    from sqlalchemy import delete as sa_delete

    now = now or utcnow()
    cutoff = now - timedelta(days=max(settings.reminders_retention_days, 1))
    result = db.execute(
        sa_delete(ReminderDelivery).where(
            ReminderDelivery.state.in_(TERMINAL),
            ReminderDelivery.updated_at < cutoff,
        )
    )
    return result.rowcount or 0


def deliverable_here(settings) -> list[str]:
    """The channels this dispatcher may claim.

    A desktop popup has to happen on the machine you are sitting at, so a
    channel may name a ``host``; ``app`` is delivered by the browser and is
    never the agent's to take. Everything else is claimable anywhere, which is
    what lets the laptop and the always-on box share one database.
    """
    host = settings.host_name
    return [
        name
        for name, ch in settings.reminder_channels.items()
        if ch.enabled and ch.kind != "app" and (not ch.host or ch.host == host)
    ]


def claim(
    db: Session,
    settings,
    now: datetime | None = None,
    limit: int = 20,
    channels: list[str] | None = None,
) -> list[ReminderDelivery]:
    """Take up to ``limit`` due reminders for this dispatcher.

    ``FOR UPDATE SKIP LOCKED`` is the whole concurrency story: two dispatchers
    over one queue each get rows the other did not. The claim is committed
    *before* anything is sent, so the failure mode of a crash mid-send is a lost
    reminder rather than a duplicated one: the wrong trade for a desktop
    popup, the right one for a phone call, and a phone call sets the rule.
    """
    now = now or utcnow()
    # The caller passes the channels it has a working sender for, which is a
    # narrower set than the configuration alone can know: see Channel.available.
    names = deliverable_here(settings) if channels is None else channels
    if not names:
        return []

    rows = db.execute(
        select(ReminderDelivery)
        .where(
            ReminderDelivery.state == PENDING,
            ReminderDelivery.fire_at_utc <= now,
            ReminderDelivery.channel.in_(names),
        )
        .order_by(ReminderDelivery.fire_at_utc)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    for row in rows:
        row.state = CLAIMED
        row.claimed_by = settings.host_name[:120]
        row.claimed_at = now
    db.commit()
    return rows


def sent_today(db: Session, channel: str, now: datetime | None = None) -> int:
    """How many this channel has already delivered in the last 24 hours."""
    from sqlalchemy import func

    now = now or utcnow()
    return int(
        db.execute(
            select(func.count(ReminderDelivery.id)).where(
                ReminderDelivery.channel == channel,
                ReminderDelivery.state == SENT,
                ReminderDelivery.sent_at >= now - timedelta(days=1),
            )
        ).scalar()
        or 0
    )
