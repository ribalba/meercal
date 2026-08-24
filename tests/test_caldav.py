"""The agent against a real CalDAV server.

Everything else in this suite tests code that never leaves the process. This
one covers the half that cannot be: discovery, the incremental listing, the
multiget, and the PUT. A mock of CalDAV would only ever prove the mock.

    tools/caldav_test_server.sh start
    make test-db test
    tools/caldav_test_server.sh stop

Skipped when either the server or the test database is missing.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MEERCAL_TEST_DB"), reason="MEERCAL_TEST_DB is not set"
)

httpx = pytest.importorskip("httpx")

URL = os.environ.get("MEERCAL_TEST_CALDAV", "http://127.0.0.1:5232")
AUTH = ("didi", "secret")
HOME = f"{URL}/didi/"
CAL = f"{HOME}team/"

STANDUP = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup@test
DTSTAMP:20260801T090000Z
DTSTART;TZID=Europe/Berlin:20260824T093000
DTEND;TZID=Europe/Berlin:20260824T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
SUMMARY:Standup
BEGIN:VALARM
TRIGGER:-PT10M
ACTION:DISPLAY
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""

TRIP = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:trip@test
DTSTAMP:20260801T090000Z
DTSTART;VALUE=DATE:20260902
DTEND;VALUE=DATE:20260921
SUMMARY:Tokyo
END:VEVENT
END:VCALENDAR
"""


def _server_is_up() -> bool:
    try:
        httpx.request("PROPFIND", URL, auth=AUTH, timeout=3)
        return True
    except Exception:
        return False


pytestmark = [
    pytestmark,
    pytest.mark.skipif(not _server_is_up(), reason=f"no CalDAV server at {URL}"),
]


@pytest.fixture(scope="module")
def server():
    """A calendar with two events on it, rebuilt from scratch."""
    mkcalendar = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:set><D:prop><D:displayname>Team</D:displayname>"
        '<C:supported-calendar-component-set><C:comp name="VEVENT"/>'
        "</C:supported-calendar-component-set></D:prop></D:set></C:mkcalendar>"
    )
    with httpx.Client(auth=AUTH, timeout=20) as c:
        c.request("DELETE", CAL)
        c.request("MKCOL", HOME)
        assert c.request("MKCALENDAR", CAL, content=mkcalendar.encode()).status_code < 400
        for name, body in (("standup.ics", STANDUP), ("trip.ics", TRIP)):
            assert c.put(CAL + name, content=body.encode(),
                         headers={"Content-Type": "text/calendar"}).status_code < 400
    yield CAL
    with httpx.Client(auth=AUTH, timeout=20) as c:
        c.request("DELETE", CAL)


@pytest.fixture(scope="module")
def account():
    from core.config import AccountConfig

    return AccountConfig(label="Radicale", kind="caldav", url=HOME,
                         username=AUTH[0], password=AUTH[1])


@pytest.fixture()
def db():
    from core.database import Base, SessionLocal, engine, init_db

    Base.metadata.drop_all(bind=engine)
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def test_discovery_finds_the_calendar(server, account):
    from agent.sync import _client

    client, _ = _client(account)
    with client:
        calendars = client.calendars(client.calendar_home(client.principal()))
    assert [c.name for c in calendars] == ["Team"]


def test_a_pass_stores_events_and_expands_them(db, server, account):
    from sqlalchemy import func, select

    from core.config import get_settings
    from core.models import Calendar, Event, Occurrence
    from agent.sync import sync_account

    sync_account(db, account, get_settings())

    calendar = db.execute(select(Calendar)).scalars().one()
    assert calendar.name == "Team"
    assert calendar.sync_token or calendar.ctag       # something to be incremental with

    events = {e.uid: e for e in db.execute(select(Event)).scalars().all()}
    assert set(events) == {"standup@test", "trip@test"}
    assert events["trip@test"].all_day is True
    assert events["standup@test"].rrule.startswith("FREQ=WEEKLY")
    # The alarm is not modelled, and must survive anyway — it is what the
    # phone that set it will look for.
    assert "BEGIN:VALARM" in events["standup@test"].raw_ics

    weekdays = db.execute(
        select(func.count(Occurrence.id)).where(Occurrence.event_id == events["standup@test"].id)
    ).scalar_one()
    assert weekdays > 100                            # a horizon of them, not one
    trip = db.execute(
        select(Occurrence).where(Occurrence.event_id == events["trip@test"].id)
    ).scalars().one()
    assert trip.span_days == 19


def test_a_second_pass_over_an_unchanged_calendar_stores_nothing(db, server, account):
    from core.config import get_settings
    from agent.sync import sync_account

    settings = get_settings()
    sync_account(db, account, settings)
    assert sync_account(db, account, settings) == 0


def test_a_queued_change_reaches_the_server_and_comes_back(db, server, account):
    from sqlalchemy import select

    from core.cal.build import new_uid
    from core.config import get_settings
    from core.expand import horizon, rebuild_series
    from core.models import Calendar, Event, PendingAction
    from agent.sync import drain_queue, sync_account

    settings = get_settings()
    sync_account(db, account, settings)
    calendar = db.execute(select(Calendar)).scalars().one()

    from datetime import datetime

    event = Event(
        calendar_id=calendar.id, uid=new_uid(), summary="Written by meercal",
        dtstart=datetime(2026, 8, 27, 13), dtend=datetime(2026, 8, 27, 14),
        dtstart_local=datetime(2026, 8, 27, 15), tz_id="Europe/Berlin", duration_s=3600,
        search_text="Written by meercal",
    )
    db.add(event)
    db.flush()
    rebuild_series(db, event, horizon(settings))
    db.add(PendingAction(kind="create", calendar_id=calendar.id, event_id=event.id, payload={}))
    db.commit()

    assert drain_queue(db, {account.name: account}) == 1
    db.refresh(event)
    assert event.url.startswith(CAL)
    assert event.etag

    # And the server really has it: a fresh pass finds the resource and updates
    # the same row rather than making a second one.
    sync_account(db, account, settings)
    rows = db.execute(
        select(Event).where(Event.summary == "Written by meercal")
    ).scalars().all()
    assert len(rows) == 1
