"""End-to-end over the real API: seed a calendar, ask for a range, get it back.

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

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    from core.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as c:      # lifespan runs init_db
        yield c
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def seeded(client):
    from core.database import SessionLocal
    from core.expand import horizon, rebuild_series
    from core.config import get_settings
    from core.models import Account, Calendar, Event

    settings = get_settings()
    with SessionLocal() as db:
        account = Account(label="Test", kind="local")
        db.add(account)
        db.flush()
        work = Calendar(account_id=account.id, url="local://work", name="Work", color="#1d6ff2")
        family = Calendar(account_id=account.id, url="local://family", name="Family",
                          color="#eb6834", visible=False)
        db.add_all([work, family])
        db.flush()

        base = datetime(2026, 8, 24)   # a Monday
        standup = Event(
            calendar_id=work.id, uid="standup", summary="Standup", search_text="Standup",
            dtstart=base + timedelta(hours=7, minutes=30),
            dtend=base + timedelta(hours=7, minutes=45),
            dtstart_local=base + timedelta(hours=9, minutes=30),
            tz_id="Europe/Berlin", duration_s=900, rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        )
        trip = Event(
            calendar_id=work.id, uid="trip", summary="Tokyo", search_text="Tokyo",
            all_day=True, dtstart=datetime(2026, 9, 2), dtend=datetime(2026, 9, 21),
            dtstart_local=datetime(2026, 9, 2), tz_id="UTC", duration_s=19 * 86400,
        )
        hidden = Event(
            calendar_id=family.id, uid="dentist", summary="Dentist", search_text="Dentist",
            dtstart=base + timedelta(hours=11), dtend=base + timedelta(hours=12),
            dtstart_local=base + timedelta(hours=13), tz_id="Europe/Berlin", duration_s=3600,
        )
        db.add_all([standup, trip, hidden])
        db.flush()
        for event in (standup, trip, hidden):
            rebuild_series(db, event, horizon(settings, datetime(2026, 8, 24)))
        db.commit()
        return {"work": work.id, "family": family.id}


def get_range(client, start="2026-08-24T00:00:00", end="2026-09-07T00:00:00", **params):
    response = client.get("/api/events", params={"start": start, "end": end, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_state_lists_the_calendars(client, seeded):
    state = client.get("/api/state").json()
    assert {c["name"] for c in state["calendars"]} == {"Work", "Family"}
    assert state["timezone"]


def test_a_range_expands_the_series_and_hides_invisible_calendars(client, seeded):
    payload = get_range(client)
    titles = [e["title"] for e in payload["events"]]
    assert titles.count("Standup") == 10          # two working weeks
    assert "Dentist" not in titles                # Family is switched off


def test_hidden_calendars_come_back_when_asked_for(client, seeded):
    payload = get_range(client, hidden="true")
    assert "Dentist" in [e["title"] for e in payload["events"]]


def test_times_are_wall_clock_in_the_display_zone(client, seeded):
    payload = get_range(client)
    standup = next(e for e in payload["events"] if e["title"] == "Standup")
    # Stored 07:30 UTC, drawn 09:30 in Berlin, and sent with no offset on it.
    assert standup["start"].endswith("T09:30:00")
    assert "+" not in standup["start"] and not standup["start"].endswith("Z")


def test_an_event_that_starts_after_the_window_but_overlaps_it_is_included(client, seeded):
    # The trip runs 2–21 September. A range ending on the 7th must still see it,
    # and must report the span it actually covers.
    trip = next(e for e in get_range(client)["events"] if e["title"] == "Tokyo")
    assert trip["all_day"] is True
    assert trip["span_days"] == 19


def test_filters_narrow_the_same_query(client, seeded):
    assert [e["title"] for e in get_range(client, q="is:span")["events"]] == ["Tokyo"]
    assert get_range(client, q="cal:family", hidden="true")["events"][0]["title"] == "Dentist"
    assert get_range(client, q="stand.*p", regex="true")["events"]


def test_search_covers_hidden_calendars_and_says_where_a_hit_lives(client, seeded):
    hits = client.get("/api/search", params={"q": "dentist"}).json()["hits"]
    assert hits and hits[0]["calendar"] == "Family"


def test_creating_an_event_writes_it_and_expands_it(client, seeded):
    body = {
        "calendar_id": seeded["work"],
        "title": "Board call",
        "start": "2026-08-26T16:00:00",
        "end": "2026-08-26T17:00:00",
    }
    created = client.post("/api/events", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["title"] == "Board call"
    assert "Board call" in [e["title"] for e in get_range(client)["events"]]

    event_id = created.json()["event_id"]
    assert client.delete(f"/api/events/{event_id}").status_code == 200
    assert "Board call" not in [e["title"] for e in get_range(client)["events"]]


def test_visibility_is_server_side_state(client, seeded):
    client.post("/api/calendars/visibility", json={"visible": [seeded["family"]]})
    titles = [e["title"] for e in get_range(client)["events"]]
    assert titles == ["Dentist"]
    client.post("/api/calendars/visibility", json={"visible": [seeded["work"], seeded["family"]]})


def test_a_backwards_range_is_refused(client, seeded):
    response = client.get("/api/events", params={"start": "2026-09-01T00:00:00",
                                                 "end": "2026-08-01T00:00:00"})
    assert response.status_code == 400


def test_sets_can_be_made_edited_and_applied(client, seeded):
    made = client.post("/api/sets", json={
        "name": "Everything", "hotkey": 0, "calendars": [seeded["work"], seeded["family"]],
    })
    assert made.status_code == 200, made.text
    set_id = made.json()["id"]

    # Renamed, re-keyed, and with a different membership — all three are the
    # reason a set is editable rather than only creatable.
    edited = client.patch(f"/api/sets/{set_id}", json={
        "name": "All of it", "hotkey": 9, "calendars": [seeded["family"]],
    })
    assert edited.status_code == 200, edited.text
    assert edited.json() == {"id": set_id, "name": "All of it", "hotkey": 9,
                             "calendars": [seeded["family"]]}

    applied = client.post(f"/api/sets/{set_id}/apply", json={})
    assert applied.json()["visible"] == [seeded["family"]]
    assert [e["title"] for e in get_range(client)["events"]] == ["Dentist"]

    client.post("/api/calendars/visibility", json={"visible": [seeded["work"], seeded["family"]]})


def test_a_key_belongs_to_one_set(client, seeded):
    first = client.post("/api/sets", json={"name": "One", "hotkey": 4, "calendars": []}).json()
    second = client.post("/api/sets", json={"name": "Two", "hotkey": 4, "calendars": []}).json()
    sets = {s["name"]: s for s in client.get("/api/state").json()["sets"]}
    # Taking a key takes it: two sets that both answer to 4 is a keyboard that
    # does something different depending on which row you looked at last.
    assert sets["Two"]["hotkey"] == 4
    assert sets["One"]["hotkey"] is None

    client.delete(f"/api/sets/{first['id']}")
    client.delete(f"/api/sets/{second['id']}")


def test_a_set_cannot_take_another_set_s_name(client, seeded):
    a = client.post("/api/sets", json={"name": "Alpha", "calendars": []}).json()
    b = client.post("/api/sets", json={"name": "Beta", "calendars": []}).json()
    clash = client.patch(f"/api/sets/{b['id']}", json={"name": "Alpha"})
    assert clash.status_code == 409
    client.delete(f"/api/sets/{a['id']}")
    client.delete(f"/api/sets/{b['id']}")


def test_clearing_a_key_needs_saying_so(client, seeded):
    made = client.post("/api/sets", json={"name": "Keyed", "hotkey": 7, "calendars": []}).json()
    # A bare PATCH leaves it alone — null is what "unchanged" looks like.
    kept = client.patch(f"/api/sets/{made['id']}", json={"name": "Keyed still"}).json()
    assert kept["hotkey"] == 7
    cleared = client.patch(f"/api/sets/{made['id']}", json={"clear_hotkey": True}).json()
    assert cleared["hotkey"] is None
    client.delete(f"/api/sets/{made['id']}")
