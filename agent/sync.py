"""One sync pass: bring every configured account down, then push what is queued.

The shape of a pass is the same for every kind of account, which is why there
is one function for it:

1. discover the calendars (or, for an .ics feed, note the one there is),
2. ask each what changed since the token we hold,
3. fetch only those resources and store them,
4. write the new token, so the next pass over a quiet calendar is one request.

The expensive first pass and the cheap thousandth are the same code; the
difference is entirely in what step 2 returns.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.cal.build import event_to_ics, patch_ics
from core.cal.ingest import (
    delete_resource,
    get_or_create_account,
    get_or_create_calendar,
    prune,
    store_resource,
    upsert_event,
)
from core.cal.parse import calendar_name, parse_calendar
from core.config import AccountConfig, Settings
from core.expand import horizon
from core.timeutil import utcnow
from core.models import Account, Calendar, Event, PendingAction
from .caldav import CalDAVClient, CalDAVError, RemoteCalendar
from .caldav import resource_url
from .google import PRINCIPAL as GOOGLE_PRINCIPAL, access_token
from .log import log

# A queued action that has failed this often is not going to start working on
# its own. It stays in the table with its error rather than being retried
# forever, so the UI can show what did not happen and why.
MAX_ATTEMPTS = 5


def _client(cfg: AccountConfig) -> tuple[CalDAVClient, str]:
    """A client and the URL discovery should start from."""
    if cfg.kind == "google":
        if not (cfg.client_id and cfg.client_secret and cfg.refresh_token):
            raise CalDAVError(
                f"{cfg.name}: Google needs client_id, client_secret and refresh_token "
                "(see agent/google.py) — an app password will not do here"
            )
        token = access_token(cfg.client_id, cfg.client_secret, cfg.refresh_token)
        base = cfg.url or GOOGLE_PRINCIPAL.format(user=cfg.username)
        return CalDAVClient(base, bearer=token), base
    base = cfg.base_url
    if not base:
        raise CalDAVError(f"{cfg.name}: no url, and kind {cfg.kind!r} has no default")
    return CalDAVClient(base, username=cfg.username, password=cfg.password), base


def sync_account(db: Session, cfg: AccountConfig, settings: Settings) -> int:
    """Everything one account has to say. Returns the number of resources stored."""
    account = get_or_create_account(db, cfg.name, cfg.kind, cfg.base_url, cfg.username)
    db.commit()
    try:
        if cfg.kind == "ics":
            stored = _sync_feed(db, account, cfg, settings)
        else:
            stored = _sync_caldav(db, account, cfg, settings)
    except Exception as exc:  # one bad account must not stop the others
        account.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
        db.commit()
        log(f"{cfg.name}: {account.last_error}", error=True)
        return 0
    account.last_error = ""
    account.last_sync_at = utcnow()
    db.commit()
    return stored


def _sync_caldav(db: Session, account: Account, cfg: AccountConfig, settings: Settings) -> int:
    window = horizon(settings)
    only = re.compile(cfg.only, re.I) if cfg.only else None
    stored = 0
    with _client(cfg)[0] as client:
        principal = client.principal()
        home = client.calendar_home(principal)
        # Discovery is where an account actually lives — iCloud sends everyone
        # to a personal host, and this is the URL worth showing when it breaks.
        account.url = home
        remote = client.calendars(home)
        log(f"{cfg.name}: {len(remote)} calendar(s) at {home}")

        for rc in remote:
            if only and not only.search(rc.name):
                continue
            cal = get_or_create_calendar(
                db, account, rc.url, rc.name, color=rc.color, tz_id=rc.tz_id, read_only=rc.read_only
            )
            db.commit()
            try:
                stored += _sync_calendar(db, client, cal, rc, window)
                cal.last_error = ""
            except Exception as exc:
                cal.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
                log(f"{cfg.name}/{rc.name}: {cal.last_error}", error=True)
            cal.last_sync_at = utcnow()
            db.commit()
    return stored


def _sync_calendar(
    db: Session,
    client: CalDAVClient,
    cal: Calendar,
    remote: RemoteCalendar,
    window: tuple[datetime, datetime],
) -> int:
    # The ctag is the cheapest possible "has anything changed at all": one
    # value for the whole collection. When it matches what we stored and we
    # already hold a sync token, there is nothing to ask.
    if remote.ctag and remote.ctag == cal.ctag and cal.sync_token:
        return 0

    listing = client.changes(cal.url, cal.sync_token)
    deleted = [c for c in listing.changes if c.deleted]
    changed = [c for c in listing.changes if not c.deleted]

    # etags we already hold are the second filter: a full listing names every
    # resource, and nearly all of them are the ones we fetched last time.
    known = {
        row.url: row.etag
        for row in db.execute(
            select(Event.url, Event.etag).where(Event.calendar_id == cal.id)
        ).all()
    }
    wanted = [c.href for c in changed if not c.etag or known.get(c.href) != c.etag]

    for change in deleted:
        delete_resource(db, cal, change.href)

    stored = 0
    for resource in client.fetch(cal.url, wanted):
        stored += store_resource(db, cal, resource.href, resource.ics, window, etag=resource.etag)

    if listing.complete:
        # Only after a listing of the whole collection — see ingest.prune.
        prune(db, cal, {c.href for c in changed})

    cal.sync_token = listing.sync_token or cal.sync_token
    cal.ctag = remote.ctag
    if stored or deleted:
        log(f"{cal.label}: +{stored} -{len(deleted)}")
    return stored


def _sync_feed(db: Session, account: Account, cfg: AccountConfig, settings: Settings) -> int:
    """A plain .ics URL: Google's "secret address", a school holiday feed, a
    colleague's published calendar. Read-only, no credentials, one GET.

    The cheap win is the ETag: a feed that has not changed answers 304 and the
    pass costs nothing.
    """
    window = horizon(settings)
    url = cfg.base_url or cfg.url
    cal = get_or_create_calendar(db, account, url, cfg.label or url, read_only=True)
    db.commit()

    headers = {"User-Agent": "meercal/agent"}
    if cal.ctag:  # a feed has no ctag of its own; the HTTP ETag lives in that column
        headers["If-None-Match"] = cal.ctag
    response = httpx.get(url, headers=headers, timeout=60.0, follow_redirects=True)
    if response.status_code == 304:
        return 0
    response.raise_for_status()

    text = response.text
    cal.ctag = response.headers.get("ETag", "")
    if not cal.name or cal.name == url:
        cal.name = calendar_name(text) or cal.name

    parsed = parse_calendar(text, default_tz=cal.tz_id or "UTC")
    seen = set()
    for event in parsed:
        upsert_event(db, cal, event, window, url=f"{url}#{event.uid}")
        seen.add(f"{url}#{event.uid}")
    prune(db, cal, seen)
    cal.last_sync_at = utcnow()
    db.commit()
    log(f"{cal.label}: {len(parsed)} event(s) from the feed")
    return len(parsed)


# --- the write path --------------------------------------------------------


def drain_queue(db: Session, accounts: dict[str, AccountConfig]) -> int:
    """Push what the user did in the UI to the server it belongs on.

    Failures are recorded on the row rather than raised: an event that could
    not be written is a thing the user has to be told about, and losing the
    queue entry would mean telling them nothing.
    """
    pending = db.execute(
        select(PendingAction).where(PendingAction.state == "queued").order_by(PendingAction.id)
    ).scalars().all()
    done = 0
    for action in pending:
        cal = db.get(Calendar, action.calendar_id) if action.calendar_id else None
        account = db.get(Account, cal.account_id) if cal else None
        cfg = accounts.get(account.label) if account else None
        if cal is None or cfg is None:
            action.state = "orphan"
            action.error = "the calendar or its account is no longer configured"
            continue
        try:
            _apply_action(db, action, cal, cfg)
            action.state = "done"
            action.error = ""
            done += 1
        except Exception as exc:
            action.attempts += 1
            action.error = f"{exc.__class__.__name__}: {exc}"[:2000]
            if action.attempts >= MAX_ATTEMPTS:
                action.state = "failed"
            log(f"queue {action.kind} #{action.id}: {action.error}", error=True)
        db.commit()
    return done


def _apply_action(db: Session, action: PendingAction, cal: Calendar, cfg: AccountConfig) -> None:
    event = db.get(Event, action.event_id) if action.event_id else None
    with _client(cfg)[0] as client:
        if action.kind == "delete":
            url = action.payload.get("url") or ""
            if url:
                client.delete(url, action.payload.get("etag", ""))
            return
        if event is None:
            raise RuntimeError("the event is gone locally; nothing to write")
        url = event.url or resource_url(cal.url, event.uid)
        ics = patch_ics(event.raw_ics, event) if event.raw_ics else event_to_ics(event)
        etag = client.put(url, ics, event.etag if action.kind == "update" else "")
        event.url, event.etag = url, etag or event.etag
