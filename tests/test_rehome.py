"""One iCloud calendar, served from two hosts, held once.

iCloud answers discovery for one account with more than one personal host.
On 2026-10-24 it named ``p106-caldav`` on one pass and ``p190-caldav`` on the
next, with the same paths, etags and sync tokens behind both. Calendars were
keyed by their whole URL, so every calendar existed twice, each copy was synced
on every other pass, and every event was drawn twice in the UI.

The server below is that account. Every discovery may name another host, and
the data behind all of them is the same.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MEERCAL_TEST_DB"), reason="MEERCAL_TEST_DB is not set"
)

httpx = pytest.importorskip("httpx")

P106 = "p106-caldav.example.invalid"
P190 = "p190-caldav.example.invalid"
HOME = "/1234/calendars/"
CAL_PATH = HOME + "family/"


def _ics(uid: str, summary: str) -> str:
    from core.timeutil import utcnow

    start = (utcnow() + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN",
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261001T000000Z",
        f"DTSTART:{start:%Y%m%dT%H%M%S}Z",
        f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}Z",
        f"SUMMARY:{summary}",
        "END:VEVENT", "END:VCALENDAR", "",
    ])


def _multistatus(*responses: str, token: str = "") -> httpx.Response:
    tail = f"<d:sync-token>{token}</d:sync-token>" if token else ""
    return httpx.Response(207, text=(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
        ' xmlns:cs="http://calendarserver.org/ns/">'
        + "".join(responses) + tail + "</d:multistatus>"
    ))


def _response(href: str, props: str = "") -> str:
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def _gone(href: str) -> str:
    """A deletion in a sync report, which has no properties left to report."""
    return f"<d:response><d:href>{href}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>"


class ICloud:
    """One account, one calendar, and a host chosen anew on every discovery."""

    def __init__(self) -> None:
        self.order = [P106, P190]
        self.discoveries = 0
        self.version = 1
        self.resources: dict[str, tuple[str, str]] = {}   # path -> (etag, text)
        self.changed: list[tuple[int, str]] = []         # (version, path)
        self.hosts_asked: list[str] = []

    def put(self, name: str, text: str) -> None:
        self.version += 1
        self.resources[CAL_PATH + name] = (f"e{self.version}", text)
        self.changed.append((self.version, CAL_PATH + name))

    def remove(self, name: str) -> None:
        self.version += 1
        del self.resources[CAL_PATH + name]
        self.changed.append((self.version, CAL_PATH + name))

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.hosts_asked.append(request.url.host)
        if "current-user-principal" in body:
            host = self.order[self.discoveries % len(self.order)]
            self.discoveries += 1
            return _multistatus(_response("/", (
                "<d:current-user-principal>"
                f"<d:href>https://{host}/1234/principal/</d:href>"
                "</d:current-user-principal>"
            )))
        if "calendar-home-set" in body:
            return _multistatus(_response(
                "/1234/principal/", f"<c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set>"
            ))
        if "resourcetype" in body:
            return _multistatus(
                _response(HOME, "<d:resourcetype><d:collection/></d:resourcetype>"),
                _response(CAL_PATH, (
                    "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                    "<d:displayname>Family</d:displayname>"
                    f"<cs:getctag>ctag-{self.version}</cs:getctag>"
                    f"<d:sync-token>tok-{self.version}</d:sync-token>"
                    '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
                    "</c:supported-calendar-component-set>"
                )),
            )
        if request.method == "PROPFIND":  # the etag listing
            return _multistatus(*(
                _response(path, f'<d:getetag>"{etag}"</d:getetag>')
                for path, (etag, _) in self.resources.items()
            ))
        if "sync-collection" in body:
            since = re.search(r"<d:sync-token>tok-(\d+)</d:sync-token>", body)
            paths = {p for v, p in self.changed if since is None or v > int(since.group(1))}
            return _multistatus(*(
                _response(path, f'<d:getetag>"{self.resources[path][0]}"</d:getetag>')
                if path in self.resources else _gone(path)
                for path in sorted(paths)
            ), token=f"tok-{self.version}")
        if "calendar-multiget" in body:
            return _multistatus(*(
                _response(path, (
                    f'<d:getetag>"{self.resources[path][0]}"</d:getetag>'
                    f"<c:calendar-data>{self.resources[path][1]}</c:calendar-data>"
                ))
                for path in re.findall(r"<d:href>(.*?)</d:href>", body)
                if path in self.resources
            ))
        return httpx.Response(405)


@pytest.fixture()
def db():
    # drop_all only drops the tables it knows of, and run alone this file has
    # not imported the models yet.
    from core import models  # noqa: F401
    from core.database import Base, SessionLocal, engine, init_db

    Base.metadata.drop_all(bind=engine)
    init_db()
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def icloud(monkeypatch):
    from agent import sync
    from agent.caldav import CalDAVClient

    fake = ICloud()
    fake.put("a.ics", _ics("a", "Birgit kommt"))
    fake.put("b.ics", _ics("b", "Dentist"))

    def client(cfg):
        c = CalDAVClient(cfg.base_url)
        c.close()
        c._client = httpx.Client(transport=httpx.MockTransport(fake.handler))
        return c, cfg.base_url

    monkeypatch.setattr(sync, "_client", client)
    return fake


@pytest.fixture()
def cfg():
    from core.config import AccountConfig

    return AccountConfig(label="iCloud", kind="caldav", url="https://caldav.example.invalid/",
                         username="u", password="p")


def sync_pass(db, cfg):
    from agent.sync import sync_account
    from core.config import get_settings
    from core.models import Account

    sync_account(db, cfg, get_settings())
    db.expire_all()
    account = db.query(Account).filter_by(label=cfg.label).one()
    assert account.last_error == ""
    return account


def calendars(db):
    from core.models import Calendar

    return db.query(Calendar).order_by(Calendar.id).all()


def events(db, cal):
    from core.models import Event

    return {e.uid: e for e in db.query(Event).filter_by(calendar_id=cal.id)}


def occurrences(db):
    from core.models import Occurrence

    return db.query(Occurrence).count()


def seed(db, cfg, host: str):
    """A calendar on ``host`` as a pass would have left it, with both events."""
    from core.cal.ingest import get_or_create_account, get_or_create_calendar, store_resource
    from core.config import get_settings
    from core.expand import horizon

    account = get_or_create_account(db, cfg.name, cfg.kind, cfg.base_url, cfg.username)
    cal = get_or_create_calendar(db, account, f"https://{host}{CAL_PATH}", "Family")
    for name, uid, summary in (("a.ics", "a", "Birgit kommt"), ("b.ics", "b", "Dentist")):
        store_resource(db, cal, f"https://{host}{CAL_PATH}{name}", _ics(uid, summary),
                       horizon(get_settings()), etag="e")
    db.commit()
    return account, cal


# --- the reported bug ------------------------------------------------------


def test_a_calendar_served_from_two_hosts_is_held_once(db, cfg, icloud):
    sync_pass(db, cfg)
    (first,) = calendars(db)
    assert first.url == f"https://{P106}{CAL_PATH}"
    assert set(events(db, first)) == {"a", "b"}

    sync_pass(db, cfg)
    (cal,) = calendars(db)
    assert cal.id == first.id
    assert cal.url == f"https://{P190}{CAL_PATH}"
    assert {e.url for e in events(db, cal).values()} == {
        f"https://{P190}{CAL_PATH}a.ics", f"https://{P190}{CAL_PATH}b.ics",
    }
    assert occurrences(db) == 2


def test_changes_and_deletions_are_found_after_the_host_moved(db, cfg, icloud):
    """Rows are matched to the server by URL. A deletion reported under the
    new host has to find the row stored under the old one, or the event stays
    on screen for good."""
    sync_pass(db, cfg)
    icloud.put("b.ics", _ics("b", "Dentist, moved"))
    icloud.remove("a.ics")

    sync_pass(db, cfg)
    (cal,) = calendars(db)
    held = events(db, cal)
    assert set(held) == {"b"}
    assert held["b"].summary == "Dentist, moved"
    assert held["b"].url == f"https://{P190}{CAL_PATH}b.ics"
    assert occurrences(db) == 1


def test_a_sync_token_survives_the_move(db, cfg, icloud):
    """The token belongs to the account, not to the host. A quiet calendar
    costs no listing on a host it has not been seen on before."""
    sync_pass(db, cfg)
    icloud.hosts_asked.clear()
    sync_pass(db, cfg)
    assert set(icloud.hosts_asked) == {"caldav.example.invalid", P190}
    assert len(icloud.hosts_asked) == 3   # principal, home, calendars, and nothing else


# --- what earlier passes left behind ----------------------------------------


def test_copies_from_earlier_passes_are_folded_into_the_oldest(db, cfg, icloud):
    from core.models import CalendarSet, CalendarSetMember, EventReminder, PendingAction

    _, keep = seed(db, cfg, P106)
    _, copy = seed(db, cfg, P190)
    family = CalendarSet(name="Family")
    db.add(family)
    db.flush()
    db.add_all([
        CalendarSetMember(set_id=family.id, calendar_id=copy.id),
        EventReminder(calendar_id=copy.id, uid="a", channels={"desktop": "off"}),
        PendingAction(kind="update", calendar_id=copy.id, state="failed", error="412"),
    ])
    db.commit()
    keep_id, copy_id = keep.id, copy.id

    icloud.order = [P190]   # the host the copy is on, so the copy matches exactly
    sync_pass(db, cfg)

    (cal,) = calendars(db)
    assert cal.id == keep_id
    assert cal.url == f"https://{P190}{CAL_PATH}"
    assert set(events(db, cal)) == {"a", "b"}
    assert occurrences(db) == 2
    assert [m.calendar_id for m in db.query(CalendarSetMember)] == [keep_id]
    assert [r.calendar_id for r in db.query(EventReminder)] == [keep_id]
    assert [a.calendar_id for a in db.query(PendingAction)] == [keep_id]
    assert copy_id not in {c.id for c in calendars(db)}


def test_a_copy_with_a_queued_write_waits_for_the_queue(db, cfg, icloud):
    """The edit is in the copy's row until the drain has sent it. Folding the
    copy first would drop the edit, so the fold waits a pass."""
    from core.models import PendingAction

    seed(db, cfg, P106)
    _, copy = seed(db, cfg, P190)
    action = PendingAction(kind="update", calendar_id=copy.id, state="queued")
    db.add(action)
    db.commit()

    icloud.order = [P190]
    sync_pass(db, cfg)
    assert len(calendars(db)) == 2

    action.state = "done"
    db.commit()
    sync_pass(db, cfg)
    assert len(calendars(db)) == 1


def test_a_queued_delete_follows_the_calendar_to_its_new_host(db, cfg):
    from core.cal.ingest import rehome_calendar
    from core.models import PendingAction

    account, cal = seed(db, cfg, P106)
    action = PendingAction(kind="delete", calendar_id=cal.id, state="queued",
                           payload={"uid": "a", "url": f"https://{P106}{CAL_PATH}a.ics"})
    db.add(action)
    db.commit()

    rehome_calendar(db, account, f"https://{P190}{CAL_PATH}")
    db.commit()
    db.expire_all()
    assert action.payload["url"] == f"https://{P190}{CAL_PATH}a.ics"


def test_another_calendar_of_the_account_is_left_alone(db, cfg):
    from core.cal.ingest import get_or_create_calendar, rehome_calendar

    account, cal = seed(db, cfg, P106)
    rehome_calendar(db, account, f"https://{P106}{HOME}work/")
    work = get_or_create_calendar(db, account, f"https://{P106}{HOME}work/", "Work")
    db.commit()
    assert [c.id for c in calendars(db)] == [cal.id, work.id]
    assert cal.url == f"https://{P106}{CAL_PATH}"
