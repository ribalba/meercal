#!/usr/bin/env python3
"""Fill an empty database with a calendar set worth looking at.

Not a fixture: the point is to make the *shape* of a real week visible without
anyone having to hand over their iCloud password to see it — several calendars
at once, meetings that repeat, two things booked over each other, and the long
events the Ribbon exists for. Everything is placed relative to today, so it is
always the current fortnight that is interesting.

    python tools/seed_demo.py            # add the demo account
    python tools/seed_demo.py --reset    # remove it first
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.cal.build import new_uid
from core.config import get_settings
from core.database import SessionLocal, init_db
from core.expand import horizon, rebuild_series
from core.timeutil import display_zone, to_utc, utcnow
from core.models import Account, Calendar, CalendarSet, CalendarSetMember, Event

LABEL = "Demo"

# name, colour, read-only
CALENDARS = [
    ("Work", "#1d6ff2", False),
    ("Family", "#eb6834", False),
    ("Kita", "#2a9d5c", True),
    ("Travel", "#a855f7", False),
    ("On-call", "#d70015", False),
    ("Conferences", "#0891b2", True),
    ("Birthdays", "#db2777", True),
]

SETS = [
    ("Work", 1, ["Work", "On-call", "Conferences"]),
    ("Family", 2, ["Family", "Kita", "Birthdays"]),
    ("Everything", 3, [name for name, _, _ in CALENDARS]),
]


def midnight(day: datetime) -> datetime:
    return datetime(day.year, day.month, day.day)


def add(
    db,
    cal: Calendar,
    title: str,
    start: datetime,
    end: datetime,
    *,
    all_day: bool = False,
    rrule: str = "",
    free: bool = False,
    location: str = "",
    people: list[str] = (),
    note: str = "",
) -> Event:
    settings = get_settings()
    # The wall times below are written the way a person would say them, so they
    # are stated in the zone the app draws in — and stored as the UTC instants
    # everything else in the database is. All-day events are dates and are not
    # converted at all; see core/timeutil.
    tz_id = "UTC" if all_day else str(display_zone(settings.timezone))
    start_utc = start if all_day else to_utc(start, tz_id)
    end_utc = end if all_day else to_utc(end, tz_id)
    event = Event(
        calendar_id=cal.id,
        uid=new_uid(),
        summary=title,
        description=note,
        location=location,
        all_day=all_day,
        transparent=free,
        dtstart=start_utc,
        dtend=end_utc,
        dtstart_local=start,
        tz_id=tz_id,
        duration_s=int((end_utc - start_utc).total_seconds()),
        rrule=rrule,
        attendees=[{"email": p, "name": p.split("@")[0].title(), "status": "ACCEPTED"} for p in people],
        search_text=f"{title} {location} {note} {' '.join(people)}",
    )
    db.add(event)
    db.flush()
    rebuild_series(db, event, horizon(settings))
    return event


def seed(reset: bool) -> None:
    init_db()
    with SessionLocal() as db:
        existing = db.execute(select(Account).where(Account.label == LABEL)).scalar_one_or_none()
        if existing and not reset:
            print(f"the {LABEL} account is already there; --reset to rebuild it")
            return
        if reset:
            if existing:
                db.delete(existing)
            # The sets are not owned by the account — they name calendars from
            # anywhere — so the cascade does not reach them, and their names
            # are unique. Clearing them is unconditional under --reset: a run
            # that failed halfway leaves the account gone and the sets behind,
            # and the next --reset has to be able to finish the job.
            for cset in db.execute(
                select(CalendarSet).where(CalendarSet.name.in_([name for name, _, _ in SETS]))
            ).scalars().all():
                db.delete(cset)
            db.commit()

        account = Account(label=LABEL, kind="local", url="", username="")
        db.add(account)
        db.flush()

        cals: dict[str, Calendar] = {}
        for position, (name, color, read_only) in enumerate(CALENDARS):
            cal = Calendar(
                account_id=account.id,
                url=f"local://demo/{name.lower()}",
                name=name,
                color=color,
                read_only=read_only,
                position=position,
            )
            db.add(cal)
            db.flush()
            cals[name] = cal

        today = midnight(utcnow())
        monday = today - timedelta(days=today.weekday())

        def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
            return monday + timedelta(days=day_offset, hours=hour, minutes=minute)

        # --- the long ones: what the Ribbon is for -------------------------
        add(db, cals["Travel"], "Tokyo — customer visit + workshop",
            monday + timedelta(days=9), monday + timedelta(days=28),
            all_day=True, location="Tokyo", note="19 days. The one thing everything else is arranged around.")
        add(db, cals["Work"], "Release freeze 0.9",
            monday - timedelta(days=3), monday + timedelta(days=18),
            all_day=True, free=True, note="Nothing merges to main. Does not block the calendar — hence the hatching.")
        add(db, cals["Kita"], "Kita closed — summer",
            monday + timedelta(days=5), monday + timedelta(days=19), all_day=True)
        add(db, cals["On-call"], "On-call",
            monday + timedelta(days=7), monday + timedelta(days=14), all_day=True, free=True)
        add(db, cals["Conferences"], "FOSDEM",
            monday + timedelta(days=33), monday + timedelta(days=36), all_day=True, location="Brussels")
        add(db, cals["Family"], "Grandparents visiting",
            monday + timedelta(days=1), monday + timedelta(days=6), all_day=True)

        # --- the repeats ---------------------------------------------------
        add(db, cals["Work"], "Standup", at(0, 9, 30), at(0, 9, 45),
            rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", people=["anna@example.com", "ben@example.com"])
        add(db, cals["Work"], "Jour fixe — platform", at(1, 14), at(1, 15),
            rrule="FREQ=WEEKLY;BYDAY=TU", location="Meet", people=["anna@example.com"])
        add(db, cals["Work"], "Sprint review", at(4, 11), at(4, 12), rrule="FREQ=WEEKLY;BYDAY=FR")
        add(db, cals["Family"], "Swimming", at(2, 16, 30), at(2, 17, 30), rrule="FREQ=WEEKLY;BYDAY=WE")
        add(db, cals["Birthdays"], "Anna's birthday",
            midnight(datetime(today.year, today.month, 1) + timedelta(days=11)),
            midnight(datetime(today.year, today.month, 1) + timedelta(days=12)),
            all_day=True, rrule="FREQ=YEARLY")

        # --- the ordinary week, including one honest double booking --------
        add(db, cals["Work"], "1:1 with Ben", at(0, 11), at(0, 11, 30), people=["ben@example.com"])
        add(db, cals["Work"], "Architecture: storage layer", at(2, 10), at(2, 11, 30),
            location="Room 2", note="Whether occurrences stay materialised.")
        add(db, cals["Family"], "Kita pickup", at(2, 10, 30), at(2, 11))   # clashes, on purpose
        add(db, cals["Work"], "Interview — backend", at(3, 13), at(3, 14))
        add(db, cals["Family"], "Dentist", at(3, 13, 30), at(3, 14, 15))   # and again
        add(db, cals["Work"], "Deploy window", at(4, 17), at(4, 19), free=True)
        add(db, cals["Family"], "Dinner with the Müllers", at(5, 19), at(5, 22), location="Kreuzberg")
        add(db, cals["Work"], "Quarterly planning", at(8, 9), at(8, 17), location="Office")
        add(db, cals["Work"], "Board call", at(15, 16), at(15, 17))
        add(db, cals["Family"], "School enrolment", at(17, 8, 30), at(17, 9, 30))

        for name, hotkey, members in SETS:
            cset = CalendarSet(name=name, hotkey=hotkey, position=hotkey)
            db.add(cset)
            db.flush()
            for member in members:
                db.add(CalendarSetMember(set_id=cset.id, calendar_id=cals[member].id))

        db.commit()
        print(f"seeded {len(CALENDARS)} calendars and {db.execute(select(Event)).scalars().all().__len__()} events")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete the demo account first")
    seed(parser.parse_args().reset)
