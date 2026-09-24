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

from core.cal.build import (
    add_exdate,
    event_to_ics,
    has_organizer,
    has_vevent,
    patch_ics,
    splice_ics,
    vevent_text,
    without_guests,
)
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
                "(see agent/google.py); an app password will not do here"
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
        # Discovery is where an account actually lives: iCloud sends everyone
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
    # etags we already hold are what makes an incremental pass cheap, and an
    # empty calendar is the one case where holding none of them is not proof
    # of anything: a token says "you have everything up to here", so a stored
    # token beside no events at all would keep the calendar empty forever.
    # That is a database restored under the agent, or a bug in the fetch path
    # that advanced the token without storing what it fetched. Distrust the
    # token there and list the whole collection; a calendar that really is
    # empty pays one extra PROPFIND a pass for it.
    known = {
        row.url: row.etag
        for row in db.execute(
            select(Event.url, Event.etag).where(Event.calendar_id == cal.id)
        ).all()
    }

    # The ctag is the cheapest possible "has anything changed at all": one
    # value for the whole collection. When it matches what we stored and we
    # already hold a sync token, there is nothing to ask.
    if remote.ctag and remote.ctag == cal.ctag and cal.sync_token and known:
        return 0

    listing = client.changes(cal.url, cal.sync_token if known else "")
    deleted = [c for c in listing.changes if c.deleted]
    changed = [c for c in listing.changes if not c.deleted]

    # Those same etags are the second filter: a full listing names every
    # resource, and nearly all of them are the ones we fetched last time.
    wanted = [c.href for c in changed if not c.etag or known.get(c.href) != c.etag]

    for change in deleted:
        delete_resource(db, cal, change.href)

    stored = 0
    for resource in client.fetch(cal.url, wanted):
        stored += store_resource(db, cal, resource.href, resource.ics, window, etag=resource.etag)

    if listing.complete:
        # Only after a listing of the whole collection; see ingest.prune.
        prune(db, cal, {c.href for c in changed})

    cal.sync_token = listing.sync_token or cal.sync_token
    # Only when the server has actually run out of pages. Recording the ctag
    # after a partial walk is what makes the shortcut above a trap: the
    # collection would look caught up, and the rest of the backlog would wait
    # for some unrelated change to move the ctag again.
    if listing.drained:
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
    """Make one queued change true on the server.

    The unit on the server is the *resource*, not the row: a recurring series
    and every instance moved out of it are one URL holding one VEVENT each (see
    core.cal.build.splice_ics). So whenever a row may share its resource, the
    resource is read first and the row's VEVENT changed inside it, and only an
    event that is alone by construction (a new one, not an override) is written
    as a resource of its own.
    """
    event = db.get(Event, action.event_id) if action.event_id else None
    # The zone a floating time is read in, as store_resource reads it: the keys
    # that match a row to its VEVENT only agree if both sides use the same one.
    default_tz = cal.tz_id or "UTC"
    with _client(cfg)[0] as client:
        if action.kind == "delete":
            _delete(db, client, cal, action.payload, default_tz)
            return
        if event is None:
            raise RuntimeError("the event is gone locally; nothing to write")
        ics = patch_ics(event.raw_ics, event) if event.raw_ics else event_to_ics(event)

        if event.url:
            # On the server already, possibly beside a master or overrides that
            # a PUT of this VEVENT alone would delete. The If-Match is the etag
            # this row was synced with, not the one just fetched: a resource
            # that changed on the server since still answers 412, and the next
            # pass brings the newer version down, as it always has.
            url = event.url
            current = client.get(url)
            if current is None:
                raise CalDAVError(
                    f"{url} is gone from the server; the next sync pass removes it here too"
                )
            etag = event.etag or current.etag
            if event.organizer and not has_organizer(current.ics):
                # A plain event becoming an invitation. RFC 6638 scheduling is
                # driven by the difference between the resource as it was and
                # as it is now, and the server compares guest lists: a guest
                # who was already on the event before it had an organiser is
                # not a new guest, so nobody is invited (iCloud, verified: the
                # organiser arrives, the guests keep NEEDS-ACTION, and no mail
                # goes out). Written in two steps, then: first the organiser
                # alone, which makes the resource a scheduling object with
                # nobody to mail, then the guests, every one of them new to it.
                alone = without_guests(ics, event.organizer)
                if alone != ics:
                    first = splice_ics(current.ics, event.uid, event.recurrence_id, alone, default_tz)
                    etag = client.put(url, first, etag)
            body = splice_ics(current.ics, event.uid, event.recurrence_id, ics, default_tz)
            etag = client.put(url, body, etag)
        elif event.recurrence_id:
            # A moved instance created here, typically an Outlook "this one
            # occurrence moved" invitation imported from a mail. Its series is
            # already a resource, and a create at that URL is a 412 on every
            # attempt: it goes *into* the series instead. Nothing there means
            # an invitation to the single instance, which is a resource of its
            # own and legal as one.
            url = _series_url(db, cal, event)
            current = client.get(url)
            if current is None:
                etag = client.put(url, ics)
            else:
                body = splice_ics(current.ics, event.uid, event.recurrence_id, ics, default_tz)
                etag = client.put(url, body, current.etag)
        else:
            url = resource_url(cal.url, event.uid)
            etag = client.put(url, ics)
        event.url, event.etag = url, etag or event.etag
        _share_etag(db, cal, url, etag, but=event.id)


def _series_url(db: Session, cal: Calendar, event: Event) -> str:
    """Where the resource an override belongs in lives: wherever a row of the
    same series was synced from, the master's first, and otherwise the URL a
    new resource for that UID would get."""
    rows = db.execute(
        select(Event.url, Event.recurrence_id).where(
            Event.calendar_id == cal.id,
            Event.uid == event.uid,
            Event.url != "",
            Event.id != event.id,
        )
    ).all()
    rows.sort(key=lambda row: row.recurrence_id != "")
    return rows[0].url if rows else resource_url(cal.url, event.uid)


def _share_etag(db: Session, cal: Calendar, url: str, etag: str, but: int | None = None) -> list[Event]:
    """Give every row synced from ``url`` the etag the server just handed back.

    They are one resource, so they have one version. A sibling left holding the
    etag from before this write would answer its own next edit with a 412 for a
    change that was ours, and the sync pass would fetch a resource it already
    has. When the server sends no etag nothing is known, and the rows keep
    theirs. Returns the rows, for a caller with more to update on them.
    """
    query = select(Event).where(Event.calendar_id == cal.id, Event.url == url)
    if but is not None:
        query = query.where(Event.id != but)
    rows = db.execute(query).scalars().all()
    if etag:
        for row in rows:
            row.etag = etag
    return rows


def _delete(db: Session, client: CalDAVClient, cal: Calendar, payload: dict, default_tz: str) -> None:
    """A deletion, which for one moved instance is an edit of its series.

    Deleting the resource is right for an event and for a whole series. For an
    override it would delete the series with it, so the override's VEVENT is
    taken out of the resource instead and the master given an EXDATE, so the
    instance does not come back at the time it was moved away from. Actions
    queued before ``recurrence_id`` was part of the payload have none, and keep
    the old meaning.
    """
    url = payload.get("url") or ""
    if not url:
        return  # never reached the server; there is nothing there to delete
    uid, recurrence_id = payload.get("uid") or "", payload.get("recurrence_id") or ""
    if not recurrence_id:
        client.delete(url, payload.get("etag", ""))
        return

    current = client.get(url)
    if current is None:
        return  # gone already, which is what was asked for
    body = splice_ics(current.ics, uid, recurrence_id, None, default_tz)
    body = add_exdate(body, uid, recurrence_id, default_tz)
    if not has_vevent(body):
        client.delete(url, current.etag)
        return
    etag = client.put(url, body, current.etag)
    rows = _share_etag(db, cal, url, etag)
    # The master's stored text is what its next edit is patched from, and the
    # EXDATE is not a property an edit writes. Left as it was, editing the
    # series before the next sync pass would put the text back without the
    # EXDATE, and the deleted instance with it.
    master_text = vevent_text(body, uid, "", default_tz)
    for row in rows:
        if master_text and row.uid == uid and not row.recurrence_id:
            row.raw_ics = master_text
