"""The reminder framework: what gets armed, when it fires, and who wins.

The tests that matter most here are not the ones about sending. Sending is
thirty lines per channel and a fake proves it. They are the ones about
*identity* and *precedence*, because both have a failure mode that is silence,
and silence is the one thing this subsystem cannot report on its own.

Skipped unless MEERCAL_TEST_DB names a database to build and drop:

    make test-db test
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MEERCAL_TEST_DB"), reason="MEERCAL_TEST_DB is not set"
)

BERLIN = "Europe/Berlin"


# --- fixtures --------------------------------------------------------------


def _settings(**over):
    """A Settings built from scratch, never from the developer's own file."""
    from core.config import Settings

    base = dict(
        timezone=BERLIN,
        reminders_enabled=True,
        reminders_grace=600,
        reminders_all_day_at="09:00",
        reminder_channels={
            "desk": {"kind": "desktop", "name": "desk"},
            "phone": {"kind": "ntfy", "name": "phone", "topic": "t"},
            "call": {"kind": "twilio", "name": "call", "account_sid": "AC", "auth_token": "x",
                     "from": "+1", "to": "+2", "ignore_quiet_hours": True},
        },
        reminder_rules=[],
    )
    base.update(over)
    return Settings(**base)


def _rule(**kw):
    kw.setdefault("channels", ["desk"])
    return kw


@pytest.fixture(scope="module")
def db():
    from core.database import Base, SessionLocal, engine, init_db

    Base.metadata.drop_all(bind=engine)
    init_db()
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def world(db):
    """A work calendar with a daily lunch and a one-off dentist appointment."""
    from core.expand import horizon, rebuild_series
    from core.models import Account, Calendar, Event, EventReminder, ReminderDelivery

    for model in (ReminderDelivery, EventReminder):
        db.query(model).delete()
    db.query(Event).delete()
    db.query(Calendar).delete()
    db.query(Account).delete()
    db.commit()

    account = Account(label="Test", kind="local")
    db.add(account)
    db.flush()
    work = Calendar(account_id=account.id, url="local://work", name="Work")
    birthdays = Calendar(account_id=account.id, url="local://bd", name="Birthdays")
    db.add_all([work, birthdays])
    db.flush()

    # 2026-09-01 is a Tuesday. Everything below is stated in Berlin wall time
    # and stored as the naive UTC the rows use (Berlin is UTC+2 in September).
    lunch = Event(
        calendar_id=work.id, uid="lunch", summary="Lunch", search_text="Lunch",
        dtstart=datetime(2026, 9, 1, 10, 30), dtend=datetime(2026, 9, 1, 11, 30),
        dtstart_local=datetime(2026, 9, 1, 12, 30), tz_id=BERLIN, duration_s=3600,
        rrule="FREQ=DAILY",
    )
    dentist = Event(
        calendar_id=work.id, uid="dentist", summary="Zahnarzt", search_text="Zahnarzt",
        dtstart=datetime(2026, 9, 1, 7, 0), dtend=datetime(2026, 9, 1, 8, 0),
        dtstart_local=datetime(2026, 9, 1, 9, 0), tz_id=BERLIN, duration_s=3600,
    )
    party = Event(
        calendar_id=birthdays.id, uid="anna", summary="Anna", search_text="Anna",
        all_day=True, dtstart=datetime(2026, 9, 3), dtend=datetime(2026, 9, 4),
        dtstart_local=datetime(2026, 9, 3), tz_id="UTC", duration_s=86400,
    )
    db.add_all([lunch, dentist, party])
    db.flush()

    window = horizon(_settings(), now=datetime(2026, 9, 1))
    for event in (lunch, dentist, party):
        rebuild_series(db, event, window)
    db.commit()
    return {"work": work, "birthdays": birthdays,
            "lunch": lunch, "dentist": dentist, "party": party}


# --- timing ----------------------------------------------------------------


def test_lead_is_measured_from_the_start(db, world):
    from core.reminders import plan

    settings = _settings(reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    now = datetime(2026, 9, 1, 6, 0)
    plans = [p for p in plan(db, settings, now=now, ahead=timedelta(hours=6)) if p.uid == "dentist"]
    assert [p.fire_at_utc for p in plans] == [datetime(2026, 9, 1, 6, 50)]


def test_all_day_is_anchored_not_led_from_midnight(db, world):
    """A birthday has no clock, so "before" has to start somewhere chosen.

    09:00 Berlin on the day is 07:00 UTC; an hour before that is 06:00 UTC. The
    bug this guards against is treating the stored midnight as an instant,
    which puts "an hour before" at 23:00 the night before.
    """
    from core.reminders import plan

    settings = _settings(reminder_rules=[_rule(name="bd", match="cal:Birthdays", lead="1h")])
    plans = plan(db, settings, now=datetime(2026, 9, 1), ahead=timedelta(days=5))
    assert [p.fire_at_utc for p in plans] == [datetime(2026, 9, 3, 6, 0)]


def test_absolute_anchor_lands_on_the_wall_clock(db, world):
    """`-1d 18:00` is 18:00 Berlin the day before, 16:00 UTC in September."""
    from core.reminders import plan

    settings = _settings(reminder_rules=[_rule(name="bd", match="cal:Birthdays", at="-1d 18:00")])
    plans = plan(db, settings, now=datetime(2026, 9, 1), ahead=timedelta(days=5))
    assert [p.fire_at_utc for p in plans] == [datetime(2026, 9, 2, 16, 0)]


def test_anchor_survives_a_dst_change(db, world):
    """The same anchor in January is 18:00 Berlin again: 17:00 UTC, not 16:00.

    Wall clock is the point: an anchor that drifts by an hour in winter is the
    bug that makes people stop trusting reminders in November.
    """
    from core.expand import horizon, rebuild_series
    from core.models import Event
    from core.reminders import plan

    winter = Event(
        calendar_id=world["birthdays"].id, uid="jan", summary="Bea", search_text="Bea",
        all_day=True, dtstart=datetime(2027, 1, 12), dtend=datetime(2027, 1, 13),
        dtstart_local=datetime(2027, 1, 12), tz_id="UTC", duration_s=86400,
    )
    db.add(winter)
    db.flush()
    rebuild_series(db, winter, horizon(_settings(), now=datetime(2027, 1, 1)))
    db.commit()

    settings = _settings(reminder_rules=[_rule(name="bd", match="Bea", at="-1d 18:00")])
    plans = plan(db, settings, now=datetime(2027, 1, 1), ahead=timedelta(days=20))
    assert [p.fire_at_utc for p in plans] == [datetime(2027, 1, 11, 17, 0)]


# --- identity: the trap this whole design is built around ------------------


def test_arming_survives_a_horizon_roll(db, world):
    """The regression test for the stable-key trap.

    ``rebuild_event`` deletes and re-inserts every occurrence row, and
    ``roll_horizon`` does it for every event in the database daily. A reminder
    keyed on ``occurrences.id``, or holding a foreign key to it, would be
    wiped within a day. Arm, roll the whole horizon, arm again: still one row.
    """
    from core.expand import roll_horizon
    from core.models import ReminderDelivery
    from core.reminders import arm, plan

    settings = _settings(reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    now = datetime(2026, 9, 1, 6, 0)

    assert arm(db, plan(db, settings, now=now, ahead=timedelta(hours=6))) == 1
    db.commit()

    roll_horizon(db, settings, force=True)

    # Nothing new to arm, and nothing lost.
    assert arm(db, plan(db, settings, now=now, ahead=timedelta(hours=6))) == 0
    db.commit()
    assert db.query(ReminderDelivery).filter_by(uid="dentist").count() == 1


def test_two_rules_wanting_the_same_moment_are_one_reminder(db, world):
    """Two rules at ten minutes before is one popup, not two.

    The identity is the moment, not the rule that asked for it. Otherwise the
    day you add a second broad rule you start getting everything twice.
    """
    from core.models import ReminderDelivery
    from core.reminders import arm, plan

    settings = _settings(
        reminder_rules=[
            _rule(name="everything", match="", lead="10m"),
            _rule(name="work too", match="cal:Work", lead="10m"),
        ]
    )
    now = datetime(2026, 9, 1, 6, 0)
    armed = arm(db, plan(db, settings, now=now, ahead=timedelta(minutes=60)))
    db.commit()
    rows = db.query(ReminderDelivery).filter_by(uid="dentist", channel="desk").all()
    assert armed == len(rows) == 1


def test_a_moved_event_rearms_and_the_stale_row_goes(db, world):
    from core.expand import horizon, rebuild_series
    from core.models import ReminderDelivery
    from core.reminders import arm, discard_stale, plan

    settings = _settings(reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    now = datetime(2026, 9, 1, 6, 0)
    arm(db, plan(db, settings, now=now, ahead=timedelta(hours=8)))
    db.commit()

    dentist = world["dentist"]
    dentist.dtstart = datetime(2026, 9, 1, 12, 0)
    dentist.dtend = datetime(2026, 9, 1, 13, 0)
    dentist.dtstart_local = datetime(2026, 9, 1, 14, 0)
    db.flush()
    rebuild_series(db, dentist, horizon(settings, now=datetime(2026, 9, 1)))
    db.commit()

    assert discard_stale(db) == 1
    arm(db, plan(db, settings, now=now, ahead=timedelta(hours=8)))
    db.commit()
    fire = [r.fire_at_utc for r in db.query(ReminderDelivery).filter_by(uid="dentist")]
    assert fire == [datetime(2026, 9, 1, 11, 50)]


# --- precedence: the lunch problem ----------------------------------------


def test_muting_a_series_beats_a_matching_rule(db, world):
    from core.models import EventReminder
    from core.reminders import plan

    db.add(EventReminder(calendar_id=world["work"].id, uid="lunch", recurrence_id="",
                         channels={"call": "off"}))
    db.commit()

    settings = _settings(
        reminder_rules=[_rule(name="all", match="", lead="10m", channels=["desk", "call"])]
    )
    plans = plan(db, _settings(**{**settings.model_dump(), "reminder_rules": settings.reminder_rules}),
                 now=datetime(2026, 9, 1, 9, 0), ahead=timedelta(hours=3))
    lunch = {(p.channel, bool(p.muted_by)) for p in plans if p.uid == "lunch"}
    assert ("desk", False) in lunch          # the popup still arrives
    assert ("call", True) in lunch           # the phone does not ring
    dentist = {p.channel for p in plans if p.uid == "dentist" and not p.muted_by}
    assert dentist == set()                  # (outside this window)


def test_a_mute_is_not_armed_but_is_still_reported(db, world):
    from core.models import EventReminder, ReminderDelivery
    from core.reminders import arm, plan

    db.add(EventReminder(calendar_id=world["work"].id, uid="lunch", recurrence_id="",
                         channels={"call": "off"}))
    db.commit()
    settings = _settings(
        reminder_rules=[_rule(name="all", match="Lunch", lead="10m", channels=["desk", "call"])]
    )
    plans = plan(db, settings, now=datetime(2026, 9, 1, 9, 0), ahead=timedelta(hours=3))
    muted = [p for p in plans if p.muted_by]
    assert muted and muted[0].muted_by == "muted on the whole series"

    arm(db, plans)
    db.commit()
    assert db.query(ReminderDelivery).filter_by(uid="lunch", channel="call").count() == 0
    assert db.query(ReminderDelivery).filter_by(uid="lunch", channel="desk").count() == 1


def test_a_later_rule_cannot_resurrect_a_mute(db, world):
    """The reason `inherit` is stored as absence and a mute as a value.

    Copying today's defaults onto the event instead would leave a stale
    snapshot: add a rule tomorrow and the event either starts ringing again or
    goes quiet for a reason nobody can find.
    """
    from core.models import EventReminder
    from core.reminders import plan

    db.add(EventReminder(calendar_id=world["work"].id, uid="lunch", recurrence_id="",
                         channels={"call": "off"}))
    db.commit()

    settings = _settings(
        reminder_rules=[
            _rule(name="old", match="Lunch", lead="10m", channels=["desk"]),
            _rule(name="brand new, calls about everything", match="", lead="10m",
                  channels=["call"]),
        ]
    )
    plans = plan(db, settings, now=datetime(2026, 9, 1, 9, 0), ahead=timedelta(hours=3))
    calls = [p for p in plans if p.uid == "lunch" and p.channel == "call"]
    assert calls and all(p.muted_by for p in calls)


def test_an_occurrence_mute_beats_the_series(db, world):
    from core.models import EventReminder
    from core.reminders import occurrence_key, plan
    from core.models import Occurrence

    occ = (
        db.query(Occurrence)
        .filter(Occurrence.event_id == world["lunch"].id,
                Occurrence.start_utc >= datetime(2026, 9, 2))
        .order_by(Occurrence.start_utc)
        .first()
    )
    key = occurrence_key(occ, world["lunch"])
    db.add(EventReminder(calendar_id=world["work"].id, uid="lunch", recurrence_id="",
                         channels={"desk": "on"}))
    db.add(EventReminder(calendar_id=world["work"].id, uid="lunch", recurrence_id=key,
                         channels={"desk": "off"}))
    db.commit()

    settings = _settings(reminder_rules=[_rule(name="all", match="Lunch", lead="10m")])
    plans = plan(db, settings, now=datetime(2026, 9, 1), ahead=timedelta(days=3))
    by_start = {p.occurrence_start_utc: p.muted_by for p in plans}
    assert by_start[occ.start_utc] == "muted on this occurrence"
    others = [v for k, v in by_start.items() if k != occ.start_utc]
    assert others and not any(others)


def test_except_removes_a_shape_of_event_at_the_rule(db, world):
    from core.reminders import plan

    settings = _settings(
        reminder_rules=[_rule(name="work", match="cal:Work", lead="10m", **{"except": "Lunch"})]
    )
    plans = plan(db, settings, now=datetime(2026, 9, 1), ahead=timedelta(days=2))
    assert {p.uid for p in plans} == {"dentist"}


def test_cancelled_events_do_not_remind(db, world):
    from core.reminders import plan

    world["dentist"].status = "CANCELLED"
    db.commit()
    settings = _settings(reminder_rules=[_rule(name="all", match="Zahnarzt", lead="10m")])
    assert plan(db, settings, now=datetime(2026, 9, 1), ahead=timedelta(days=2)) == []
    world["dentist"].status = ""
    db.commit()


# --- the event's own alarms -----------------------------------------------


def test_valarm_supplies_the_lead(db, world):
    from core.reminders import plan

    world["dentist"].alarms = [{"trigger": "-PT45M", "related": "START", "action": "DISPLAY"}]
    db.commit()
    settings = _settings(reminder_rules=[_rule(name="v", match="Zahnarzt", lead="valarm")])
    plans = plan(db, settings, now=datetime(2026, 9, 1, 5, 0), ahead=timedelta(hours=4))
    assert [p.fire_at_utc for p in plans] == [datetime(2026, 9, 1, 6, 15)]
    world["dentist"].alarms = []
    db.commit()


# --- delivery --------------------------------------------------------------


def test_grace_fires_late_then_gives_up(db, world):
    from core.models import ReminderDelivery
    from core.reminders import arm, mark_missed, plan

    settings = _settings(reminders_grace=600,
                         reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    # Fires at 06:50. Five minutes late is inside the grace window...
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 55), ahead=timedelta(hours=1)))
    db.commit()
    assert db.query(ReminderDelivery).filter_by(state="pending").count() == 1

    # ...twenty is not.
    assert mark_missed(db, settings, now=datetime(2026, 9, 1, 7, 10)) == 1
    db.commit()
    assert db.query(ReminderDelivery).filter_by(state="missed").count() == 1


def test_claim_takes_only_this_host_s_channels(db, world):
    from core.reminders import arm, claim, deliverable_here, plan

    settings = _settings(
        reminders_host="thinkpad",
        reminder_channels={
            "desk": {"kind": "desktop", "name": "desk", "host": "thinkpad"},
            "other": {"kind": "desktop", "name": "other", "host": "nas"},
            "phone": {"kind": "ntfy", "name": "phone", "topic": "t"},
        },
        reminder_rules=[_rule(name="all", match="Zahnarzt", lead="10m",
                              channels=["desk", "other", "phone"])],
    )
    assert set(deliverable_here(settings)) == {"desk", "phone"}

    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 45), ahead=timedelta(hours=1)))
    db.commit()
    taken = claim(db, settings, now=datetime(2026, 9, 1, 6, 55))
    assert {r.channel for r in taken} == {"desk", "phone"}
    assert all(r.claimed_by == "thinkpad" for r in taken)


def test_a_dispatcher_that_cannot_deliver_leaves_the_row_alone(db, world):
    """A container must not swallow the desktop's reminders.

    meercal's own compose file offers to run the agent in a container, and the
    queue is shared, so a dispatcher with no session bus claiming `desktop`
    rows would take them away from the machine that could have shown them, and
    fail them there. It must not claim them at all.
    """
    from core.reminders import PENDING, arm, claim, plan
    from core.models import ReminderDelivery

    settings = _settings(reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 45), ahead=timedelta(hours=1)))
    db.commit()

    # No senders: nothing is claimable, whatever the configuration says.
    assert claim(db, settings, now=datetime(2026, 9, 1, 6, 55), channels=[]) == []
    assert db.query(ReminderDelivery).filter_by(state=PENDING).count() == 1

    # The host that can deliver still gets it.
    assert len(claim(db, settings, now=datetime(2026, 9, 1, 6, 55), channels=["desk"])) == 1


def test_a_failing_channel_retries_then_stops(db, world):
    from agent.remind import dispatch_pass
    from core.models import ReminderDelivery
    from core.reminders import MAX_ATTEMPTS, arm, plan

    settings = _settings(reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 49), ahead=timedelta(hours=1)))
    db.commit()

    class Broken:
        def send(self, note):
            raise RuntimeError("nope")

    for _ in range(MAX_ATTEMPTS):
        dispatch_pass(db, settings, {"desk": Broken()}, now=datetime(2026, 9, 1, 6, 51))
    row = db.query(ReminderDelivery).filter_by(uid="dentist").one()
    assert row.state == "failed" and row.attempts == MAX_ATTEMPTS and "nope" in row.error


def test_a_permanent_failure_does_not_retry(db, world):
    from agent.channels import ChannelError
    from agent.remind import dispatch_pass
    from core.models import ReminderDelivery
    from core.reminders import arm, plan

    settings = _settings(reminder_rules=[_rule(name="ten", match="Zahnarzt", lead="10m")])
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 49), ahead=timedelta(hours=1)))
    db.commit()

    class Wrong:
        def send(self, note):
            raise ChannelError("bad token", permanent=True)

    dispatch_pass(db, settings, {"desk": Wrong()}, now=datetime(2026, 9, 1, 6, 51))
    row = db.query(ReminderDelivery).filter_by(uid="dentist").one()
    assert row.state == "failed" and row.attempts == 1


def test_the_daily_cap_stops_a_runaway_rule(db, world):
    from agent.remind import dispatch_pass
    from core.models import ReminderDelivery
    from core.reminders import arm, plan

    # A generous grace, so that what stops the second and third call is the cap
    # and unambiguously not the clock.
    settings = _settings(
        reminder_channels={"call": {"kind": "twilio", "name": "call", "max_per_day": 1,
                                    "account_sid": "AC", "auth_token": "x",
                                    "from": "+1", "to": "+2", "grace": 10 * 86400}},
        reminder_rules=[_rule(name="lunch", match="Lunch", lead="10m", channels=["call"])],
    )
    # Lunch is daily: three instances are armed over three days.
    armed = arm(db, plan(db, settings, now=datetime(2026, 9, 1, 9, 0), ahead=timedelta(days=3)))
    db.commit()
    assert armed == 3

    sent = []
    class Fake:
        def send(self, note):
            sent.append(note.title)

    dispatch_pass(db, settings, {"call": Fake()}, now=datetime(2026, 9, 3, 11, 0))
    assert len(sent) == 1
    assert db.query(ReminderDelivery).filter_by(state="capped").count() == 2


def test_quiet_hours_hold_a_reminder_but_not_the_event_inside_them(db, world):
    """A reminder for a morning meeting is held until the window ends; a
    reminder about something happening *during* quiet hours still fires."""
    from agent.remind import _quiet_verdict
    from core.models import ReminderDelivery

    settings = _settings(reminders_quiet_hours="23:00-07:00")
    cfg = settings.reminder_channels["desk"]

    # Fires 05:00 UTC = 07:00 Berlin... use 03:00 UTC = 05:00 Berlin, inside.
    held = ReminderDelivery(
        calendar_id=1, uid="x", occurrence_start_utc=datetime(2026, 9, 1, 8, 0),
        all_day=False, channel="desk", fire_at_utc=datetime(2026, 9, 1, 3, 0),
    )
    assert _quiet_verdict(held, cfg, settings) == "defer"

    # The event itself is at 05:30 Berlin, inside the window, so it fires.
    during = ReminderDelivery(
        calendar_id=1, uid="y", occurrence_start_utc=datetime(2026, 9, 1, 3, 30),
        all_day=False, channel="desk", fire_at_utc=datetime(2026, 9, 1, 3, 0),
    )
    assert _quiet_verdict(during, cfg, settings) == "send"

    # A channel that exists to wake you ignores the window entirely.
    call = settings.reminder_channels["call"]
    assert _quiet_verdict(held, call, settings) == "send"


# --- the API ---------------------------------------------------------------
#
# These exist because the endpoints below were once written without them, and a
# column removed from the model left a `GET` raising AttributeError that every
# other test in this file passed straight through. The panel is the surface
# people actually use; it deserves the same cover as the engine.


@pytest.fixture
def client(db, world):
    from fastapi.testclient import TestClient

    import app.routers.reminders as router
    from app.main import app as fastapi_app

    # The router reads the configuration once at import, as the others do.
    # Point it at this test's channels and rules for the duration.
    settings = _settings(
        reminder_rules=[
            _rule(name="everything", match="", lead="10m", channels=["desk"]),
            _rule(name="work", match="cal:Work", lead="1h", channels=["desk", "phone"]),
        ]
    )
    before = router.settings
    router.settings = settings
    with TestClient(fastapi_app) as c:
        yield c
    router.settings = before


def test_get_event_reminders_resolves_the_chain(client, world):
    body = client.get(f"/api/events/{world['dentist'].id}/reminders").json()
    assert body["recurring"] is False
    by_name = {c["name"]: c for c in body["channels"]}
    assert by_name["desk"]["state"] == "inherit" and by_name["desk"]["effective"] is True
    assert by_name["desk"]["why"] in ("everything", "work")
    assert by_name["call"]["effective"] is False   # no rule sends to it


def test_put_then_get_round_trips_a_mute(client, world):
    eid = world["dentist"].id
    assert client.put(f"/api/events/{eid}/reminders",
                      json={"channels": {"desk": "off"}}).status_code == 200
    by_name = {c["name"]: c for c in client.get(f"/api/events/{eid}/reminders").json()["channels"]}
    assert by_name["desk"]["state"] == "off"
    assert by_name["desk"]["effective"] is False
    assert by_name["desk"]["why"] == "muted on this event"

    # Clearing every opinion removes the row rather than storing an empty one.
    assert client.put(f"/api/events/{eid}/reminders", json={"channels": {}}).json() == {
        "cleared": True, "scope": "series"
    }
    by_name = {c["name"]: c for c in client.get(f"/api/events/{eid}/reminders").json()["channels"]}
    assert by_name["desk"]["state"] == "inherit"


def test_put_rejects_what_it_cannot_honour(client, world):
    eid = world["dentist"].id
    bad_channel = client.put(f"/api/events/{eid}/reminders", json={"channels": {"pigeon": "off"}})
    assert bad_channel.status_code == 400 and "pigeon" in bad_channel.json()["detail"]

    bad_state = client.put(f"/api/events/{eid}/reminders", json={"channels": {"desk": "maybe"}})
    assert bad_state.status_code == 400

    bad_lead = client.put(f"/api/events/{eid}/reminders",
                          json={"channels": {"desk": "on"}, "leads": ["10 fortnights"]})
    assert bad_lead.status_code == 400


def test_muting_clears_what_is_already_queued(client, db, world):
    """A mute set five minutes before the call is a mute, not a preference for
    next time, so it has to reach the rows already in the queue."""
    from core.models import ReminderDelivery
    from core.reminders import PENDING, arm, plan

    settings = _settings(reminder_rules=[_rule(name="all", match="Zahnarzt", lead="10m")])
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 0), ahead=timedelta(hours=8)))
    db.commit()
    assert db.query(ReminderDelivery).filter_by(uid="dentist", state=PENDING).count() == 1

    client.put(f"/api/events/{world['dentist'].id}/reminders", json={"channels": {"desk": "off"}})
    db.expire_all()
    assert db.query(ReminderDelivery).filter_by(uid="dentist", state=PENDING).count() == 0


def test_the_in_app_channel_claims_its_own(client, db, world):
    from core.models import ReminderDelivery
    from core.reminders import SENT, arm, plan

    settings = _settings(
        reminder_channels={"window": {"kind": "app", "name": "window"},
                           "desk": {"kind": "desktop", "name": "desk"}},
        reminder_rules=[_rule(name="all", match="Zahnarzt", lead="10m",
                              channels=["window", "desk"])],
    )
    import app.routers.reminders as router
    router.settings = settings
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 55), ahead=timedelta(hours=1)))
    db.commit()

    # Nothing is due yet against the real clock, so the browser gets nothing,
    # and above all does not get the desktop's row.
    assert client.post("/api/reminders/claim", json={}).json()["reminders"] == []

    from core.timeutil import utcnow

    for row in db.query(ReminderDelivery).all():
        row.fire_at_utc = utcnow()
    db.commit()

    got = client.post("/api/reminders/claim", json={}).json()["reminders"]
    assert [r["channel"] for r in got] == ["window"]
    db.expire_all()
    assert db.query(ReminderDelivery).filter_by(channel="window").one().state == SENT
    assert db.query(ReminderDelivery).filter_by(channel="desk").one().state == "pending"


def test_snooze_and_dismiss(client, db, world):
    from core.models import ReminderDelivery
    from core.reminders import arm, plan

    settings = _settings(reminder_rules=[_rule(name="all", match="Zahnarzt", lead="10m")])
    arm(db, plan(db, settings, now=datetime(2026, 9, 1, 6, 0), ahead=timedelta(hours=8)))
    db.commit()
    rid = db.query(ReminderDelivery).filter_by(uid="dentist").one().id

    body = client.post(f"/api/reminders/{rid}/snooze", json={"minutes": 5}).json()
    assert body["state"] == "pending"
    assert client.post(f"/api/reminders/{rid}/dismiss", json={}).json()["state"] == "dismissed"
    assert client.post("/api/reminders/999999/dismiss", json={}).status_code == 404
