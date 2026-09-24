"""The queue, drained against a server that holds whole resources.

A recurring series and every instance moved out of it are one resource on a
CalDAV server: one URL, one etag, one VCALENDAR with a VEVENT each. The
database keeps a row per VEVENT. Everything here is about the gap between the
two, which is where the agent used to write one row *as* the resource:

* an instance moved in Outlook and imported from the invitation was created at
  the series' URL with If-None-Match, and iCloud answered 412 on every attempt;
* an edit to the series was a PUT of the series alone, which deleted every
  moved instance on the server, and an edit to a moved instance deleted the
  series;
* deleting one moved instance deleted the whole series.

The server below is a dictionary behind ``httpx.MockTransport`` that honours
If-Match and If-None-Match the way a real one does, so a test that passes
against it is a request a real server would have accepted. ``test_caldav.py``
covers the real conversation, and is skipped without one.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MEERCAL_TEST_DB"), reason="MEERCAL_TEST_DB is not set"
)

httpx = pytest.importorskip("httpx")

BASE = "https://caldav.example.invalid/"
CAL_URL = BASE + "1234/calendars/work/"
# Deliberately not the name resource_url would make up for the UID: iCloud files
# an accepted invitation under a name of its own, and the series has to be found
# by where it was synced from.
SERIES_URL = CAL_URL + "F00D-invitation.ics"
UID = "040000008200E00074C5B7101A82E008@outlook"

BERLIN = [
    "BEGIN:VTIMEZONE", "TZID:Europe/Berlin",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST",
    "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET",
    "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE",
]


def _monday(weeks_ahead: int) -> datetime:
    """A Monday relative to today, so that the series is always inside the
    horizon the routers expand into, whenever this runs."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks_ahead)
    return datetime(monday.year, monday.month, monday.day)


FIRST = _monday(1).replace(hour=9, minute=30)
INSTANCE = FIRST + timedelta(weeks=1)               # the one that was moved
MOVED_TO = INSTANCE.replace(hour=13)
KEY = f"{INSTANCE:%Y%m%dT%H%M%S}"
STAMP = "%Y%m%dT%H%M%S"


def master_lines(summary: str = "Weekly sync") -> list[str]:
    return [
        "BEGIN:VEVENT", f"UID:{UID}", "DTSTAMP:20260801T090000Z",
        f"DTSTART;TZID=Europe/Berlin:{FIRST:{STAMP}}",
        f"DTEND;TZID=Europe/Berlin:{FIRST + timedelta(minutes=30):{STAMP}}",
        "RRULE:FREQ=WEEKLY",
        f"SUMMARY:{summary}",
        "BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "END:VALARM",
        "END:VEVENT",
    ]


def moved_lines(summary: str = "Weekly sync (moved)") -> list[str]:
    return [
        "BEGIN:VEVENT", f"UID:{UID}", "DTSTAMP:20260801T090000Z",
        f"RECURRENCE-ID;TZID=Europe/Berlin:{KEY}",
        f"DTSTART;TZID=Europe/Berlin:{MOVED_TO:{STAMP}}",
        f"DTEND;TZID=Europe/Berlin:{MOVED_TO + timedelta(minutes=30):{STAMP}}",
        f"SUMMARY:{summary}",
        "END:VEVENT",
    ]


def calendar(*parts: list[str]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Apple Inc.//iOS 18//EN"]
    lines += [ln for part in parts for ln in part]
    return "\r\n".join([*lines, "END:VCALENDAR"]) + "\r\n"


class Server:
    """Resources by URL, with the preconditions a CalDAV server enforces."""

    def __init__(self) -> None:
        self.resources: dict[str, tuple[str, str]] = {}   # url -> (etag, text)
        self.requests: list[httpx.Request] = []
        self._version = 0

    def hold(self, url: str, text: str) -> str:
        self._version += 1
        etag = f"v{self._version}"
        self.resources[url] = (etag, text)
        return etag

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, str(r.url)) for r in self.requests]

    def last(self, method: str) -> httpx.Request:
        return [r for r in self.requests if r.method == method][-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        held = self.resources.get(url)
        if_match = request.headers.get("If-Match")
        if request.method == "GET":
            if held is None:
                return httpx.Response(404)
            return httpx.Response(200, text=held[1], headers={"ETag": f'"{held[0]}"'})
        if request.headers.get("If-None-Match") == "*" and held is not None:
            return httpx.Response(412)
        if if_match and (held is None or if_match.strip('"') != held[0]):
            return httpx.Response(412)
        if request.method == "PUT":
            etag = self.hold(url, request.content.decode())
            return httpx.Response(204 if held else 201, headers={"ETag": f'"{etag}"'})
        if request.method == "DELETE":
            if held is None:
                return httpx.Response(404)
            del self.resources[url]
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture()
def db():
    from core.database import Base, SessionLocal, engine, init_db

    Base.metadata.drop_all(bind=engine)
    init_db()
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def server(monkeypatch):
    """Every client the agent opens talks to the same in-memory server."""
    from agent import sync
    from agent.caldav import CalDAVClient

    fake = Server()

    def client(cfg):
        c = CalDAVClient(cfg.base_url)
        c.close()
        c._client = httpx.Client(transport=httpx.MockTransport(fake.handler))
        return c, cfg.base_url

    monkeypatch.setattr(sync, "_client", client)
    return fake


@pytest.fixture()
def world(db):
    """An iCloud-shaped account with one calendar, and the config the agent
    would read for it."""
    from core.config import AccountConfig
    from core.models import Account, Calendar

    account = Account(label="iCloud", kind="icloud", url=BASE)
    db.add(account)
    db.flush()
    cal = Calendar(account_id=account.id, url=CAL_URL, name="Work", tz_id="Europe/Berlin")
    db.add(cal)
    db.commit()
    cfg = AccountConfig(label="iCloud", kind="caldav", url=BASE, username="u", password="p")
    return {"cal": cal, "accounts": {"iCloud": cfg}}


def _window():
    from core.config import get_settings
    from core.expand import horizon

    return horizon(get_settings())


def synced(db, cal, server: Server, url: str, text: str):
    """A resource on the server and the rows a sync pass would have made of it."""
    from core.cal.ingest import store_resource

    etag = server.hold(url, text)
    store_resource(db, cal, url, text, _window(), etag=etag)
    db.commit()
    return etag


def rows(db, cal):
    """The series' rows, by recurrence key ("" is the master)."""
    from sqlalchemy import select

    from core.models import Event

    db.expire_all()
    found = db.execute(
        select(Event).where(Event.calendar_id == cal.id, Event.uid == UID)
        .order_by(Event.recurrence_id)
    ).scalars().all()
    return {e.recurrence_id: e for e in found}


def queue(db, cal, kind: str, event=None, payload: dict | None = None):
    from core.models import PendingAction

    action = PendingAction(kind=kind, calendar_id=cal.id,
                           event_id=event.id if event is not None else None,
                           payload=payload or {})
    db.add(action)
    db.commit()
    return action


def vevents(text: str) -> dict[str, object]:
    from core.cal.parse import parse_calendar

    return {e.recurrence_id: e for e in parse_calendar(text, default_tz="Europe/Berlin")}


# --- creating --------------------------------------------------------------


def test_a_moved_instance_created_here_is_written_into_its_series(db, server, world):
    """The reported bug, pending action #34: a PUT to the series' URL with
    If-None-Match, answered 412 five times and then given up on."""
    from agent.sync import drain_queue
    from core.cal.ingest import upsert_event
    from core.cal.parse import parse_calendar

    cal = world["cal"]
    etag = synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines()))
    # What the import endpoint does with an invitation carrying one instance.
    (parsed,) = parse_calendar(calendar(BERLIN, moved_lines("Moved by Outlook")), "Europe/Berlin")
    override = upsert_event(db, cal, parsed, _window())
    queue(db, cal, "create", override)

    assert drain_queue(db, world["accounts"]) == 1

    assert server.calls() == [("GET", SERIES_URL), ("PUT", SERIES_URL)]
    put = server.last("PUT")
    assert put.headers["If-Match"] == f'"{etag}"'
    assert "If-None-Match" not in put.headers
    body = put.content.decode()
    written = vevents(body)
    assert set(written) == {"", KEY}
    assert written[KEY].summary == "Moved by Outlook"
    assert written[""].alarms            # the series went back with its alarm
    assert "BEGIN:VTIMEZONE" in body

    new_etag, _ = server.resources[SERIES_URL]
    held = rows(db, cal)
    assert (held[KEY].url, held[KEY].etag) == (SERIES_URL, new_etag)
    # The series is the same resource, so it is the same version now too.
    assert held[""].etag == new_etag


def test_a_moved_instance_with_no_series_on_the_server_is_a_resource_of_its_own(db, server, world):
    """An invitation to one instance of somebody else's series: legal, and the
    only thing there is to write."""
    from agent.caldav import resource_url
    from agent.sync import drain_queue
    from core.cal.ingest import upsert_event
    from core.cal.parse import parse_calendar

    cal = world["cal"]
    (parsed,) = parse_calendar(calendar(BERLIN, moved_lines()), "Europe/Berlin")
    override = upsert_event(db, cal, parsed, _window())
    queue(db, cal, "create", override)

    assert drain_queue(db, world["accounts"]) == 1

    target = resource_url(CAL_URL, UID)
    assert server.calls() == [("GET", target), ("PUT", target)]
    put = server.last("PUT")
    assert put.headers["If-None-Match"] == "*"
    assert "If-Match" not in put.headers
    assert set(vevents(put.content.decode())) == {KEY}
    assert rows(db, cal)[KEY].url == target


def test_a_new_event_is_still_created_without_reading_first(db, server, world):
    from agent.caldav import resource_url
    from agent.sync import drain_queue
    from core.models import Event

    cal = world["cal"]
    start = FIRST.replace(hour=15)
    event = Event(
        calendar_id=cal.id, uid="fresh@meercal", summary="Written by meercal",
        dtstart=start - timedelta(hours=2), dtend=start - timedelta(hours=1),
        dtstart_local=start, tz_id="Europe/Berlin", duration_s=3600,
    )
    db.add(event)
    db.commit()
    queue(db, cal, "create", event)

    assert drain_queue(db, world["accounts"]) == 1

    target = resource_url(CAL_URL, "fresh@meercal")
    assert server.calls() == [("PUT", target)]
    assert server.last("PUT").headers["If-None-Match"] == "*"


# --- updating --------------------------------------------------------------


def test_editing_a_series_keeps_the_instances_moved_out_of_it(db, server, world):
    from agent.sync import drain_queue

    cal = world["cal"]
    etag = synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    master = rows(db, cal)[""]
    master.summary = "Weekly sync, new room"
    master.sequence += 1
    db.commit()
    queue(db, cal, "update", master)

    assert drain_queue(db, world["accounts"]) == 1

    assert server.calls() == [("GET", SERIES_URL), ("PUT", SERIES_URL)]
    put = server.last("PUT")
    assert put.headers["If-Match"] == f'"{etag}"'
    written = vevents(put.content.decode())
    assert set(written) == {"", KEY}
    assert written[""].summary == "Weekly sync, new room"
    assert written[KEY].summary == "Weekly sync (moved)"
    held = rows(db, cal)
    assert held[""].etag == held[KEY].etag == server.resources[SERIES_URL][0]


def test_editing_a_series_changed_on_the_server_since_is_still_a_conflict(db, server, world):
    """The read before the write must not become a way round If-Match: the
    etag sent is the one the row was synced with, so a series somebody changed
    in the meantime answers 412 and the next sync pass brings theirs down."""
    from agent.sync import drain_queue

    cal = world["cal"]
    etag = synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    server.hold(SERIES_URL, calendar(BERLIN, master_lines("Renamed on the phone"), moved_lines()))
    master = rows(db, cal)[""]
    master.summary = "Renamed here"
    db.commit()
    action = queue(db, cal, "update", master)

    assert drain_queue(db, world["accounts"]) == 0

    assert server.last("PUT").headers["If-Match"] == f'"{etag}"'
    db.refresh(action)
    assert action.state == "queued" and action.attempts == 1 and "412" in action.error
    assert vevents(server.resources[SERIES_URL][1])[""].summary == "Renamed on the phone"


def test_editing_a_row_whose_resource_is_gone_fails_rather_than_recreating_it(db, server, world):
    from agent.sync import drain_queue

    cal = world["cal"]
    synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    del server.resources[SERIES_URL]
    master = rows(db, cal)[""]
    master.summary = "Renamed here"
    db.commit()
    action = queue(db, cal, "update", master)

    assert drain_queue(db, world["accounts"]) == 0

    assert server.calls() == [("GET", SERIES_URL)]
    db.refresh(action)
    assert "gone" in action.error


# --- deleting --------------------------------------------------------------


def test_deleting_a_moved_instance_edits_its_series_instead(db, server, world):
    from agent.sync import drain_queue

    cal = world["cal"]
    etag = synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    override = rows(db, cal)[KEY]
    # What DELETE /api/events/{id} queues, and then does to the row.
    queue(db, cal, "delete", payload={"uid": UID, "url": override.url, "etag": override.etag,
                                      "recurrence_id": KEY})
    db.delete(override)
    db.commit()

    assert drain_queue(db, world["accounts"]) == 1

    assert server.calls() == [("GET", SERIES_URL), ("PUT", SERIES_URL)]
    put = server.last("PUT")
    assert put.headers["If-Match"] == f'"{etag}"'
    body = put.content.decode()
    written = vevents(body)
    assert set(written) == {""}
    assert f"EXDATE;TZID=Europe/Berlin:{KEY}" in body
    assert written[""].exdate == INSTANCE.isoformat()

    master = rows(db, cal)[""]
    assert master.etag == server.resources[SERIES_URL][0]
    # The next edit of the series is patched from this text, and must not put
    # the instance back by writing the series without its EXDATE.
    assert f"EXDATE;TZID=Europe/Berlin:{KEY}" in master.raw_ics


def test_a_moved_instance_then_an_edit_of_its_series_keeps_the_instance_deleted(db, server, world):
    from agent.sync import drain_queue

    cal = world["cal"]
    synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    held = rows(db, cal)
    queue(db, cal, "delete", payload={"uid": UID, "url": SERIES_URL, "etag": held[KEY].etag,
                                      "recurrence_id": KEY})
    db.delete(held[KEY])
    held[""].summary = "Weekly sync, new room"
    db.commit()
    queue(db, cal, "update", held[""])

    assert drain_queue(db, world["accounts"]) == 2

    written = vevents(server.resources[SERIES_URL][1])
    assert set(written) == {""}
    assert written[""].summary == "Weekly sync, new room"
    assert written[""].exdate == INSTANCE.isoformat()


def test_deleting_the_only_vevent_of_a_resource_deletes_the_resource(db, server, world):
    from agent.sync import drain_queue

    cal = world["cal"]
    etag = synced(db, cal, server, SERIES_URL, calendar(BERLIN, moved_lines()))
    queue(db, cal, "delete", payload={"uid": UID, "url": SERIES_URL, "etag": etag,
                                      "recurrence_id": KEY})

    assert drain_queue(db, world["accounts"]) == 1

    assert server.calls() == [("GET", SERIES_URL), ("DELETE", SERIES_URL)]
    assert server.last("DELETE").headers["If-Match"] == f'"{etag}"'
    assert SERIES_URL not in server.resources


@pytest.mark.parametrize(
    "extra", [{"recurrence_id": ""}, {}], ids=["a series", "queued before recurrence_id"]
)
def test_deleting_a_series_still_deletes_the_resource(db, server, world, extra):
    from agent.sync import drain_queue

    cal = world["cal"]
    etag = synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    queue(db, cal, "delete", payload={"uid": UID, "url": SERIES_URL, "etag": etag, **extra})

    assert drain_queue(db, world["accounts"]) == 1

    assert server.calls() == [("DELETE", SERIES_URL)]
    assert SERIES_URL not in server.resources


# --- syncing while something waits in the queue ------------------------------


def test_a_sync_keeps_a_moved_instance_that_is_still_waiting_to_be_written(db, server, world):
    """The agent syncs before it drains. A series that changed on the server
    in between used to delete the override imported here before it was sent."""
    from core.cal.ingest import store_resource, upsert_event
    from core.cal.parse import parse_calendar

    cal = world["cal"]
    synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines()))
    (parsed,) = parse_calendar(calendar(BERLIN, moved_lines()), "Europe/Berlin")
    upsert_event(db, cal, parsed, _window())       # no url: never on the server
    db.commit()

    renamed = calendar(BERLIN, master_lines("Renamed on the phone"))
    store_resource(db, cal, SERIES_URL, renamed, _window(), etag=server.hold(SERIES_URL, renamed))
    db.commit()

    held = rows(db, cal)
    assert set(held) == {"", KEY}
    assert held[KEY].url == ""
    assert held[""].summary == "Renamed on the phone"


def test_a_sync_still_drops_a_moved_instance_the_server_no_longer_has(db, server, world):
    from core.cal.ingest import store_resource

    cal = world["cal"]
    synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    assert set(rows(db, cal)) == {"", KEY}

    put_back = calendar(BERLIN, master_lines())
    store_resource(db, cal, SERIES_URL, put_back, _window(), etag=server.hold(SERIES_URL, put_back))
    db.commit()

    assert set(rows(db, cal)) == {""}


# --- the endpoint ------------------------------------------------------------


def test_deleting_a_moved_instance_in_the_ui_leaves_its_slot_empty(db, server, world):
    """Without the EXDATE the master would draw the instance again, at the
    time it had been moved away from, the moment the override row went."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.main import app
    from core.models import Occurrence, PendingAction
    from core.timeutil import to_utc

    cal = world["cal"]
    synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines(), moved_lines()))
    held = rows(db, cal)
    master_id, override_id = held[""].id, held[KEY].id
    original_slot = to_utc(INSTANCE, "Europe/Berlin")

    def starts() -> set[datetime]:
        db.expire_all()
        return set(db.execute(
            select(Occurrence.start_utc).where(Occurrence.event_id == master_id)
        ).scalars())

    assert original_slot not in starts()          # the override holds it
    assert to_utc(INSTANCE + timedelta(weeks=1), "Europe/Berlin") in starts()

    # Nothing of ours left open: starting the app runs the schema bootstrap,
    # whose ALTER TABLE would wait on this session's locks for ever.
    db.commit()
    with TestClient(app) as client:
        assert client.delete(f"/api/events/{override_id}").status_code == 200

    assert set(rows(db, cal)) == {""}
    assert rows(db, cal)[""].exdate == INSTANCE.isoformat()
    assert original_slot not in starts()
    assert to_utc(INSTANCE + timedelta(weeks=1), "Europe/Berlin") in starts()

    (action,) = db.execute(select(PendingAction)).scalars().all()
    assert action.kind == "delete"
    assert action.payload["recurrence_id"] == KEY
    assert action.payload["url"] == SERIES_URL
    assert server.requests == []                  # the web app never speaks CalDAV


def test_deleting_a_moved_instance_that_never_reached_the_server_gives_its_slot_back(db, server, world):
    """An imported invitation still waiting in the queue is not on the server,
    so there is nothing there to exclude: the series on the server still has
    the instance at its old time. An EXDATE here would hide a meeting the
    server, and every other device, still shows."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.main import app
    from core.cal.ingest import upsert_event
    from core.cal.parse import parse_calendar
    from core.models import Occurrence
    from core.timeutil import to_utc

    cal = world["cal"]
    synced(db, cal, server, SERIES_URL, calendar(BERLIN, master_lines()))
    (parsed,) = parse_calendar(calendar(BERLIN, moved_lines()), "Europe/Berlin")
    override = upsert_event(db, cal, parsed, _window())
    queue(db, cal, "create", override)
    held = rows(db, cal)
    master_id, override_id = held[""].id, held[KEY].id
    assert held[KEY].url == ""
    original_slot = to_utc(INSTANCE, "Europe/Berlin")

    def starts() -> set[datetime]:
        db.expire_all()
        return set(db.execute(
            select(Occurrence.start_utc).where(Occurrence.event_id == master_id)
        ).scalars())

    assert original_slot not in starts()

    db.commit()
    with TestClient(app) as client:
        assert client.delete(f"/api/events/{override_id}").status_code == 200

    assert set(rows(db, cal)) == {""}
    assert rows(db, cal)[""].exdate == ""
    assert original_slot in starts()
    assert server.requests == []


# --- becoming an invitation --------------------------------------------------


def _plain_event_with_a_guest(uid: str, extra: list[str] = ()) -> str:
    return calendar(BERLIN, [
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20260801T090000Z",
        f"DTSTART;TZID=Europe/Berlin:{FIRST:{STAMP}}",
        f"DTEND;TZID=Europe/Berlin:{FIRST + timedelta(minutes=30):{STAMP}}",
        "SUMMARY:Checkin",
        'ATTENDEE;CN="Michael";PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:michael@example.com',
        *extra,
        "END:VEVENT",
    ])


def _organised_here(db, uid: str):
    """The row for ``uid`` after the panel put the account on it as organiser."""
    from sqlalchemy import select

    from core.models import Event

    db.expire_all()
    event = db.execute(select(Event).where(Event.uid == uid)).scalar_one()
    event.organizer = "me@example.com"
    event.attendees = [
        {"name": "", "email": "me@example.com", "role": "CHAIR", "status": "ACCEPTED"},
        *event.attendees,
    ]
    event.sequence += 1
    db.commit()
    return event


def test_an_organiser_added_to_a_plain_event_invites_its_guests_in_two_steps(db, server, world):
    # iCloud invites the guests a write *adds*. A guest already on the event
    # before it had an organiser is not added by the write that brings the
    # organiser, so one PUT would leave them NEEDS-ACTION with no mail sent.
    from agent.sync import drain_queue

    cal = world["cal"]
    url = CAL_URL + "plain.ics"
    synced(db, cal, server, url, _plain_event_with_a_guest("plain@meercal"))
    event = _organised_here(db, "plain@meercal")
    queue(db, cal, "update", event)

    assert drain_queue(db, world["accounts"]) == 1

    assert server.calls() == [("GET", url), ("PUT", url), ("PUT", url)]
    first, second = [r for r in server.requests if r.method == "PUT"]
    alone = vevents(first.content.decode())[""]
    assert alone.organizer == "me@example.com"
    assert [a["email"] for a in alone.attendees] == ["me@example.com"]
    full = vevents(second.content.decode())[""]
    assert full.organizer == "me@example.com"
    assert [a["email"] for a in full.attendees] == ["me@example.com", "michael@example.com"]
    # The second write is conditional on the first having landed, not on the
    # etag the row was synced with, which the first write made stale.
    final = int(server.resources[url][0][1:])
    assert second.headers["If-Match"] == f'"v{final - 1}"'
    from sqlalchemy import select

    from core.models import Event

    db.expire_all()
    assert db.execute(select(Event.etag).where(Event.uid == "plain@meercal")).scalar_one() == f"v{final}"


def test_an_invitation_that_already_has_an_organiser_is_written_once(db, server, world):
    from agent.sync import drain_queue

    cal = world["cal"]
    url = CAL_URL + "theirs.ics"
    synced(db, cal, server, url, _plain_event_with_a_guest(
        "theirs@meercal", ["ORGANIZER;CN=Me:mailto:me@example.com"]))
    event = _organised_here(db, "theirs@meercal")
    queue(db, cal, "update", event)

    assert drain_queue(db, world["accounts"]) == 1
    assert server.calls() == [("GET", url), ("PUT", url)]
