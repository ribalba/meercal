"""meercal-agent: the half that holds the credentials.

It runs on your machine, next to nothing in particular: unlike meerail's agent
it does not have to sit beside a Bridge, but the reason for the split is the
same. The password to your family's iCloud calendar should live in a file you
own, in a process you started, and not in a container that also serves HTTP.
The agent and the web app share nothing but Postgres: the agent writes, the
app reads, neither calls the other.

    python -m agent.main            # sync forever, every [agent] interval
    python -m agent.main --once     # one pass, then exit (cron, or a check)
    python -m agent.main --test     # prove every connection, change nothing

It also runs the reminder loops (``agent/remind.py``) on a thread of their own,
because they tick every 30 seconds and a sync pass takes minutes, and one loop
doing both would either sync far too often or remind far too late. Reminders
belong to the agent for the same reason CalDAV does: this is the half with the
credentials, and the half with a session bus to put a notification on.

    python -m agent.remind --test   # one notification per channel
    python -m agent.remind --next   # what would fire in the next 24 hours
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import threading
import time
from datetime import datetime

from sqlalchemy import select

from core.config import config_path, get_settings
from core.database import SessionLocal, init_db
from core.expand import roll_horizon
from core.models import Setting
from core.version import VERSION
from .caldav import CalDAVError
from .log import log
from .sync import drain_queue, sync_account, _client


def check_config_permissions() -> None:
    """Refuse to run on a world-readable configuration file.

    It holds calendar passwords in plaintext. This is the one check worth
    failing on rather than warning about: a warning printed at three in the
    morning by a service nobody is watching protects nobody.
    """
    path = config_path()
    if path is None or not path.is_file():
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        raise SystemExit(
            f"meercal: {path} is readable by other users and holds passwords.\n"
            f"  chmod 600 {path}"
        )


def test_connections(settings) -> int:
    """Every configured account, tried once, with a verdict per line."""
    failures = 0
    for cfg in settings.accounts:
        if cfg.kind == "ics":
            log(f"{cfg.name}: feed {cfg.base_url} (no credentials)")
            continue
        try:
            client, base = _client(cfg)
            with client:
                principal = client.principal()
                home = client.calendar_home(principal)
                calendars = client.calendars(home)
            names = ", ".join(c.name for c in calendars[:6]) or "(none)"
            extra = f" +{len(calendars) - 6} more" if len(calendars) > 6 else ""
            log(f"{cfg.name}: OK, {len(calendars)} calendar(s): {names}{extra}")
        except (CalDAVError, Exception) as exc:
            failures += 1
            log(f"{cfg.name}: FAILED, {exc}", error=True)
    return failures


def reload_settings():
    """The configuration as it is on disk *now*.

    ``get_settings`` is cached for the life of the process, which is right for
    a web server answering requests and wrong for a daemon whose config file is
    a bind mount somebody edits while it runs.
    """
    get_settings.cache_clear()
    return get_settings()


def one_pass(settings) -> None:
    accounts = {cfg.name: cfg for cfg in settings.accounts}
    with SessionLocal() as db:
        for cfg in settings.accounts:
            sync_account(db, cfg, settings)
        pushed = drain_queue(db, accounts)
        if pushed:
            log(f"pushed {pushed} change(s)")
        rolled = roll_horizon(db, settings)
        if rolled >= 0:
            log(f"horizon rolled: {rolled} occurrence(s) re-expanded")


def start_reminders(settings) -> None:
    """Run the reminder loops beside the sync loop, on their own thread.

    A thread rather than a second process: they want the same configuration,
    the same session factory and the same log, and asking the user to start two
    services to get reminders would be one service too many. A daemon thread so
    that Ctrl-C still ends the program at once: there is no work in flight
    worth waiting for, and the queue is in Postgres either way.

    Reminders are also runnable alone (``python -m agent.remind``), which is
    what you want when the machine you sit at is not the machine that syncs.
    """
    from . import remind

    if not remind.active(settings):
        return

    def loop() -> None:
        try:
            remind.run_forever(reload_settings)
        except Exception as exc:  # never take the sync loop down with it
            log(f"reminders: loop stopped: {type(exc).__name__}: {exc}", error=True)

    threading.Thread(target=loop, name="meercal-reminders", daemon=True).start()


def sync_requested(db) -> bool:
    """Did somebody press Refresh in the UI since the last pass?"""
    row = db.get(Setting, "sync_request")
    if row is None:
        return False
    seen = db.get(Setting, "sync_seen")
    stamp = (row.value or {}).get("at", "")
    if seen is not None and (seen.value or {}).get("at") == stamp:
        return False
    if seen is None:
        db.add(Setting(key="sync_seen", value={"at": stamp}))
    else:
        seen.value = {"at": stamp}
    db.commit()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(prog="meercal-agent")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--test", action="store_true", help="check every connection and exit")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(VERSION)
        return 0

    check_config_permissions()
    settings = reload_settings()

    # A one-shot run with nothing configured is a mistake worth an exit code.
    # The long-running one is not: see the loop below.
    if (args.once or args.test) and not settings.accounts:
        log("no [[agent.account]] blocks configured, nothing to sync", error=True)
        log(f"add one to {config_path()} (see meercal.example.toml)", error=True)
        return 2

    if args.test:
        return 1 if test_connections(settings) else 0

    init_db()

    if args.once:
        one_pass(settings)
        return 0

    log(f"meercal-agent {VERSION}: {len(settings.accounts)} account(s), every {settings.agent_interval}s")
    start_reminders(settings)
    idle_notice = 0.0
    while True:
        started = time.monotonic()
        # Re-read the file every pass. The agent is a container in the normal
        # install, and its configuration is a bind mount the user edits from
        # outside, so adding an account should not need a restart to take, and
        # a container that exits because there are none yet would spend the
        # afternoon in a restart loop instead of waiting for one.
        settings = reload_settings()
        if settings.accounts:
            one_pass(settings)
        elif time.monotonic() - idle_notice > 3600:
            idle_notice = time.monotonic()
            log(f"no accounts configured yet, waiting. Add one to {config_path()}.")

        # Wait out the interval, but wake early if the UI asked for a refresh.
        # Polling one small row a second is nothing next to a sync pass, and it
        # is what makes the button in the sidebar feel like it did something.
        deadline = started + settings.agent_interval
        while time.monotonic() < deadline:
            time.sleep(1.0)
            with SessionLocal() as db:
                if sync_requested(db):
                    log("refresh requested")
                    break


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
