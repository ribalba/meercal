"""Dropping an .ics on the window, end to end.

The interesting cases are not "does icalendar parse this" -- test_parse covers
that -- but what the endpoint does around it: refusing a file that is not a
calendar before it creates one, updating rather than duplicating on a second
import of the same UID, and queueing a write for a calendar that has a server
behind it, which is the thing that stops the next sync pass from pruning
everything just imported.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MEERCAL_TEST_DB"), reason="MEERCAL_TEST_DB is not set"
)

fastapi_testclient = pytest.importorskip("fastapi.testclient")


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
X-WR-CALNAME:School year
BEGIN:VEVENT
UID:term-start@school
SUMMARY:Term starts
DTSTART;VALUE=DATE:20260901
DTEND;VALUE=DATE:20260902
END:VEVENT
BEGIN:VEVENT
UID:assembly@school
SUMMARY:Assembly
DTSTART;TZID=Europe/Berlin:20260904T090000
DTEND;TZID=Europe/Berlin:20260904T100000
RRULE:FREQ=WEEKLY;BYDAY=FR
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    from core.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def upload(text: str = ICS, name: str = "school.ics"):
    return {"file": (name, text.encode("utf-8"), "text/calendar")}


# --- what the file says it is ---------------------------------------------


def test_preview_reads_the_file_without_writing_anything(client):
    before = len(client.get("/api/state").json()["calendars"])
    body = client.post("/api/import/preview", files=upload()).json()

    assert body["events"] == 2
    assert body["recurring"] == 1
    assert body["all_day"] == 1
    # X-WR-CALNAME wins over the file name: it is what the calendar calls
    # itself, and it is what the "new calendar" field is prefilled with.
    assert body["name"] == "School year"
    assert body["first"] == "2026-09-01"
    assert len(client.get("/api/state").json()["calendars"]) == before


def test_preview_falls_back_to_the_file_name(client):
    stripped = ICS.replace("X-WR-CALNAME:School year\n", "")
    body = client.post(
        "/api/import/preview", files=upload(stripped, "Holidays 2026.ics")
    ).json()
    assert body["name"] == "Holidays 2026"


def test_a_file_that_is_not_a_calendar_is_refused(client):
    response = client.post("/api/import/preview", files=upload("hello", "notes.txt"))
    assert response.status_code == 400
    assert "BEGIN:VCALENDAR" in response.json()["detail"]


def test_a_calendar_with_no_events_in_it_says_so(client):
    empty = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//EN\nEND:VCALENDAR\n"
    response = client.post("/api/import/preview", files=upload(empty))
    assert response.status_code == 400
    assert "no events" in response.json()["detail"]


# --- the writing -----------------------------------------------------------


def test_import_into_a_new_calendar(client):
    response = client.post(
        "/api/import", files=upload(), data={"new_calendar": "School year"}
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["created"], body["updated"]) == (2, 0)
    # Local, so nothing is waiting to be written anywhere.
    assert body["queued"] == 0

    cal_id = body["calendar"]["id"]
    state = client.get("/api/state").json()
    assert body["calendar"]["name"] == "School year"
    assert any(a["label"] == "Imported" and a["kind"] == "local" for a in state["accounts"])

    # And the events are drawable, which means occurrences were expanded and
    # not merely that rows exist.
    events = client.get(
        "/api/events",
        params={"start": "2026-09-01T00:00:00", "end": "2026-09-08T00:00:00", "cals": cal_id},
    ).json()["events"]
    assert {e["title"] for e in events} == {"Term starts", "Assembly"}


def test_importing_the_same_file_twice_updates_rather_than_duplicates(client):
    first = client.post(
        "/api/import", files=upload(), data={"new_calendar": "Twice"}
    ).json()
    cal_id = first["calendar"]["id"]

    changed = ICS.replace("SUMMARY:Assembly", "SUMMARY:Assembly (moved)")
    second = client.post(
        "/api/import", files=upload(changed), data={"calendar_id": cal_id}
    ).json()

    assert (second["created"], second["updated"]) == (0, 2)
    assert second["calendar"]["id"] == cal_id
    events = client.get(
        "/api/events",
        params={"start": "2026-09-04T00:00:00", "end": "2026-09-05T00:00:00", "cals": cal_id},
    ).json()["events"]
    assert [e["title"] for e in events] == ["Assembly (moved)"]


def test_a_bad_file_leaves_no_calendar_behind(client):
    before = len(client.get("/api/state").json()["calendars"])
    response = client.post(
        "/api/import", files=upload("hello", "notes.txt"), data={"new_calendar": "Ghost"}
    )
    assert response.status_code == 400
    assert len(client.get("/api/state").json()["calendars"]) == before


def test_import_needs_a_calendar(client):
    assert client.post("/api/import", files=upload()).status_code == 400


def test_a_read_only_calendar_is_refused(client):
    from core.database import SessionLocal
    from core.models import Account, Calendar

    with SessionLocal() as db:
        account = Account(label="Elsewhere", kind="local")
        db.add(account)
        db.flush()
        cal = Calendar(account_id=account.id, url="local://ro", name="Feed", read_only=True)
        db.add(cal)
        db.commit()
        cal_id = cal.id

    response = client.post("/api/import", files=upload(), data={"calendar_id": cal_id})
    assert response.status_code == 403


def test_importing_into_a_server_calendar_queues_the_writes(client):
    """The agent owns the credentials, so it does the PUTs.

    Without this the events would live here and nowhere else, and the next full
    sync pass -- which prunes whatever the server did not send -- would delete
    every one of them.
    """
    from sqlalchemy import select

    from core.database import SessionLocal
    from core.models import Account, Calendar, PendingAction

    with SessionLocal() as db:
        account = Account(label="iCloud", kind="caldav", url="https://example.invalid")
        db.add(account)
        db.flush()
        cal = Calendar(account_id=account.id, url="https://example.invalid/home", name="Home")
        db.add(cal)
        db.commit()
        cal_id = cal.id

    body = client.post("/api/import", files=upload(), data={"calendar_id": cal_id}).json()
    assert body["queued"] == 2

    with SessionLocal() as db:
        queued = db.execute(
            select(PendingAction).where(PendingAction.calendar_id == cal_id)
        ).scalars().all()
        assert len(queued) == 2
        # Nothing here has been on the server yet, so every one is a create.
        assert {a.kind for a in queued} == {"create"}
        assert all(a.state == "queued" for a in queued)


def test_cp1252_is_read_rather_than_refused(client):
    """An export from older Windows software is not UTF-8 and is still a file
    somebody needs to import."""
    text = ICS.replace("SUMMARY:Assembly", "SUMMARY:Café duty")
    response = client.post(
        "/api/import/preview",
        files={"file": ("win.ics", text.encode("cp1252"), "text/calendar")},
    )
    assert response.status_code == 200
    assert "Café duty" in response.json()["titles"]
