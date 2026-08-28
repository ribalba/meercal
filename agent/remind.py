"""meercal's reminder loop, the only part of the program that speaks first.

Two loops and a table between them. The **scheduler** re-derives every reminder
due in the near future from the rules and the materialised occurrences, and
inserts them with ``ON CONFLICT DO NOTHING``; the **dispatcher** claims what is
due with ``FOR UPDATE SKIP LOCKED`` and hands it to a channel. Neither holds
state between passes, which is what makes a restart a non-event.

It lives in the agent for the same reason CalDAV does: this is the half that
holds credentials and the half that has a session bus. A container cannot show
you a desktop notification, and the Twilio token should not be in one.

    python -m agent.remind              # run the loops (also run by agent.main)
    python -m agent.remind --once       # one pass of each, then exit
    python -m agent.remind --test       # one real notification per channel
    python -m agent.remind --next 24h   # what would fire, and what is muted

``--next`` resolves the same precedence chain the scheduler does and prints the
muted reminders too, because "why didn't it ring" has to have an answer that is
one command long.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

from sqlalchemy.orm import Session

from core.database import SessionLocal, init_db
from core.reminders import (
    CAPPED,
    FAILED,
    MAX_ATTEMPTS,
    MISSED,
    PENDING,
    SENT,
    arm,
    claim,
    deliverable_here,
    discard_stale,
    mark_missed,
    plan,
    purge,
    sent_today,
)
from core.timeutil import (
    display_zone,
    from_utc,
    humanize_lead,
    in_quiet_hours,
    parse_duration,
    utcnow,
)
from core.version import VERSION
from .channels import Notification, build
from .log import log


# --- the scheduler ---------------------------------------------------------


def schedule_pass(db: Session, settings, now=None) -> int:
    """Arm everything due between now and the far edge of the longest rule."""
    now = now or utcnow()
    plans = plan(db, settings, now=now)
    dropped = discard_stale(db)
    armed = arm(db, plans)
    retired = mark_missed(db, settings, now=now)
    db.commit()
    if dropped:
        log(f"reminders: {dropped} stale reminder(s) dropped, their event moved")
    if retired:
        log(f"reminders: {retired} missed (outside the grace window)")
    return armed


# --- the dispatcher --------------------------------------------------------


def _notification(row, settings, now) -> Notification:
    payload = row.payload or {}
    late = (now - row.fire_at_utc).total_seconds() > 90
    fields = dict(payload.get("fields") or {})
    body = payload.get("body", "")
    if late:
        # Say so rather than pretending. A reminder delivered forty minutes
        # after the fact that still says "in 10 minutes" is worse than one that
        # admits it: you act on the first one and miss the thing.
        when = humanize_lead(row.occurrence_start_utc - now)
        fields["when"] = when
        body = f"{when}{fields.get('calendar_suffix', '')}{fields.get('location_suffix', '')}".strip()
    return Notification(
        title=payload.get("title") or payload.get("summary") or "meercal",
        body=body,
        fields=fields,
        channel=row.channel,
        fire_at=row.fire_at_utc,
        start_utc=row.occurrence_start_utc,
        all_day=row.all_day,
        late=late,
    )


def _quiet_verdict(row, channel_cfg, settings) -> str:
    """``send``, ``defer`` or ``send``: what quiet hours say about this one.

    Quiet hours exist so that a reminder about tomorrow does not wake you
    tonight. They are not a reason to be silent about something happening
    *during* them: if the event itself falls inside the window, being woken is
    the entire point of having asked. So only a reminder whose event is safely
    outside is held, and it is held until the window ends rather than dropped.
    Dropping it would mean no reminder at all for an early meeting, which is
    the opposite of what the setting is for.
    """
    window = settings.quiet_window
    if window is None or channel_cfg.ignore_quiet_hours:
        return "send"
    tz = display_zone(settings.timezone)
    if not in_quiet_hours(from_utc(row.fire_at_utc, tz), window):
        return "send"
    start_wall = row.occurrence_start_utc if row.all_day else from_utc(row.occurrence_start_utc, tz)
    if in_quiet_hours(start_wall, window):
        return "send"
    return "defer"


def _defer_past_quiet(row, settings) -> None:
    """Move a held reminder to the moment quiet hours end."""
    from datetime import datetime

    tz = display_zone(settings.timezone)
    _, end = settings.quiet_window
    wall = from_utc(row.fire_at_utc, tz)
    target = wall.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if target <= wall:
        target += timedelta(days=1)
    from core.timeutil import to_utc

    row.fire_at_utc = to_utc(datetime.combine(target.date(), target.time()), str(tz))
    row.state = PENDING
    row.claimed_by = ""
    row.claimed_at = None


def dispatch_pass(db: Session, settings, senders: dict, now=None) -> int:
    """Deliver whatever is due. Returns how many went out.

    ``now`` is a parameter rather than a call to the clock so that every
    decision in one pass (is it late, is it inside quiet hours, has the cap
    been reached) is made against the same instant. A pass that reads the
    clock four times can disagree with itself across a minute boundary, and it
    is untestable besides.
    """
    now = now or utcnow()
    rows = claim(db, settings, now=now, channels=list(senders))
    if not rows:
        return 0

    sent = 0
    for row in rows:
        cfg = settings.reminder_channels.get(row.channel)
        sender = senders.get(row.channel)
        if cfg is None or sender is None:
            row.state = FAILED
            row.error = f"channel {row.channel!r} is no longer configured"
            continue

        # A per-channel grace, applied here because this is where the channel
        # is known. A desktop popup about a meeting that started ten minutes
        # ago is useful; a phone call about one is worse than silence.
        grace = cfg.grace or settings.reminders_grace
        if (now - row.fire_at_utc).total_seconds() > grace:
            row.state = MISSED
            row.error = f"more than {grace}s late for {row.channel}"
            continue

        if _quiet_verdict(row, cfg, settings) == "defer":
            _defer_past_quiet(row, settings)
            log(f"reminders: {row.channel} held until quiet hours end: {row.payload.get('title', '')}")
            continue

        if cfg.max_per_day and sent_today(db, row.channel, now) >= cfg.max_per_day:
            row.state = CAPPED
            row.error = f"daily cap of {cfg.max_per_day} reached for {row.channel}"
            log(f"reminders: {row.channel} hit its daily cap of {cfg.max_per_day}", error=True)
            continue

        note = _notification(row, settings, now)
        try:
            sender.send(note)
        except Exception as exc:  # a bad channel must not stop the others
            row.attempts += 1
            row.error = f"{type(exc).__name__}: {exc}"[:2000]
            permanent = getattr(exc, "permanent", False)
            row.state = FAILED if (permanent or row.attempts >= MAX_ATTEMPTS) else PENDING
            row.claimed_by, row.claimed_at = "", None
            log(f"reminders: {row.channel} failed: {row.error}", error=True)
            continue

        row.state = SENT
        row.sent_at = now
        row.error = ""
        # Flushed, not left for the commit at the end: this session factory has
        # autoflush off, and the daily cap counts sent rows, so without this a
        # single pass could place every call a channel was capped to six of.
        db.flush()
        sent += 1
        log(f"reminders: {row.channel} <- {note.title} ({note.fields.get('when', '')})")

    db.commit()
    return sent


# --- one pass of both ------------------------------------------------------


def one_pass(db: Session, settings, senders: dict, now=None) -> tuple[int, int]:
    now = now or utcnow()
    armed = schedule_pass(db, settings, now=now)
    sent = dispatch_pass(db, settings, senders, now=now)
    return armed, sent


def build_senders(settings) -> dict:
    """A sender per channel this dispatcher can actually deliver on.

    Two filters, and the second is the one that matters when the agent runs in
    a container: a channel this process structurally cannot send (a desktop
    notification with no session bus) is dropped here so that its rows are
    never claimed, and stay in the queue for the dispatcher that can.
    """
    out = {}
    for name in deliverable_here(settings):
        sender = build(settings.reminder_channels[name])
        if sender is None:
            continue
        if not sender.available():
            log(
                f"reminders: {name} cannot be delivered from here, leaving it "
                f"for another host ({_why_unavailable(settings.reminder_channels[name])})"
            )
            continue
        out[name] = sender
    return out


def _why_unavailable(cfg) -> str:
    if cfg.kind == "desktop":
        return "no session bus, so notify-send would go nowhere"
    return f"kind {cfg.kind!r} is not usable in this process"


def active(settings) -> bool:
    """Is there anything to do at all?

    With no rules or no channels the loops are never started, which is why
    ``enabled`` is a kill switch rather than the setting that turns the feature
    on: configuring a rule is what turns it on.
    """
    return bool(
        settings.reminders_enabled and settings.reminder_rules and settings.reminder_channels
    )


# --- the two commands you actually type ------------------------------------


def test_channels(settings) -> int:
    """One real notification through every configured channel, verdict per line.

    The analogue of ``make agent-test``, and for the same reason: the moment to
    find out that a token is wrong is not the morning you miss the appointment.
    """
    if not settings.reminder_channels:
        log("no [reminders.channel.*] blocks configured", error=True)
        return 2
    failures = 0
    for name, cfg in settings.reminder_channels.items():
        if cfg.kind == "app":
            log(f"{name}: in-app, delivered by the meercal window, nothing to test here")
            continue
        if not cfg.enabled:
            log(f"{name}: disabled")
            continue
        if cfg.host and cfg.host != settings.host_name:
            log(f"{name}: belongs to host {cfg.host!r}, not this one ({settings.host_name})")
            continue
        sender = build(cfg)
        if sender is None:
            failures += 1
            log(f"{name}: FAILED, no sender for kind {cfg.kind!r}", error=True)
            continue
        try:
            log(f"{name}: OK, {sender.check()}")
        except Exception as exc:
            failures += 1
            log(f"{name}: FAILED, {exc}", error=True)
    return failures


def show_next(settings, ahead: timedelta) -> int:
    """What would fire in the next while, muted reminders included.

    The dry run that makes a config change safe to make at 23:00, and the
    answer to "why didn't it ring": a muted line says which level silenced it.
    """
    tz = display_zone(settings.timezone)
    with SessionLocal() as db:
        plans = plan(db, settings, ahead=ahead)
    if not plans:
        log(f"nothing due in the next {humanize_lead(ahead)[3:]}")
        return 0

    width = max(len(p.payload.get("summary", "")) for p in plans)
    width = min(max(width, 12), 44)
    for p in plans:
        when = from_utc(p.fire_at_utc, tz).strftime("%a %H:%M")
        title = (p.payload.get("summary") or "")[:width].ljust(width)
        if p.muted_by:
            log(f"{when}  {title}  {p.channel:<8} · {p.muted_by}  [muted]")
        else:
            log(f"{when}  {title}  {p.channel:<8} · {p.rule}")
    muted = sum(1 for p in plans if p.muted_by)
    log(f"{len(plans) - muted} reminder(s), {muted} muted")
    return 0


# --- the loop --------------------------------------------------------------


def run_forever(get_config) -> None:
    """Tick until killed. ``get_config`` re-reads the file each pass."""
    settings = get_config()
    senders = build_senders(settings)
    log(
        f"reminders: {len(settings.reminder_rules)} rule(s), "
        f"{len(senders)} channel(s) on {settings.host_name}, every {settings.reminders_tick}s"
    )
    signature = _signature(settings)
    idle_notice = 0.0

    while True:
        started = time.monotonic()
        settings = get_config()
        # The config is a file somebody edits while this runs. Rebuild the
        # senders only when it actually changed, so an ordinary pass does not
        # pay for a comparison nobody asked for.
        if _signature(settings) != signature:
            signature = _signature(settings)
            senders = build_senders(settings)
            log(f"reminders: configuration reloaded: {len(senders)} channel(s)")

        if active(settings):
            try:
                with SessionLocal() as db:
                    one_pass(db, settings, senders)
                    purge(db, settings)
                    db.commit()
            except Exception as exc:  # a bad pass must not end the loop
                log(f"reminders: pass failed: {type(exc).__name__}: {exc}", error=True)
        elif time.monotonic() - idle_notice > 3600:
            idle_notice = time.monotonic()
            log("reminders: no rules configured, waiting")

        time.sleep(max(1.0, settings.reminders_tick - (time.monotonic() - started)))


def _signature(settings) -> str:
    import json

    return json.dumps(
        {
            "channels": {n: c.model_dump(mode="json") for n, c in settings.reminder_channels.items()},
            "rules": [r.model_dump(mode="json") for r in settings.reminder_rules],
            "host": settings.reminders_host,
        },
        sort_keys=True,
        default=str,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meercal-remind")
    parser.add_argument("--once", action="store_true", help="one pass of each loop, then exit")
    parser.add_argument("--test", action="store_true", help="one notification per channel, then exit")
    parser.add_argument(
        "--next", metavar="WINDOW", nargs="?", const="24h",
        help="print what would fire in the next WINDOW (default 24h), then exit",
    )
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print(VERSION)
        return 0

    from .main import check_config_permissions, reload_settings

    check_config_permissions()
    settings = reload_settings()

    if args.test:
        return 1 if test_channels(settings) else 0

    if args.next:
        try:
            ahead = abs(parse_duration(args.next))
        except ValueError as exc:
            log(str(exc), error=True)
            return 2
        init_db()
        return show_next(settings, ahead)

    if not active(settings):
        log("no reminder rules configured, nothing to do", error=True)
        log("add a [[reminders.rule]] block (see meercal.example.toml)", error=True)
        return 2

    init_db()
    if args.once:
        with SessionLocal() as db:
            armed, sent = one_pass(db, settings, build_senders(settings))
        log(f"reminders: {armed} armed, {sent} sent")
        return 0

    run_forever(reload_settings)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
