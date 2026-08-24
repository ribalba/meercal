from datetime import datetime, timedelta

from core.expand import instances, recurrence_key
from core.models import Event


def make(**kw) -> Event:
    """An unsaved Event with the fields expansion actually reads."""
    start = kw.pop("start", datetime(2026, 8, 24, 7, 30))       # UTC
    local = kw.pop("local", datetime(2026, 8, 24, 9, 30))       # Europe/Berlin
    duration = kw.pop("duration_s", 3600)
    event = Event(
        calendar_id=1,
        uid="u1",
        recurrence_id="",
        dtstart=start,
        dtend=start + timedelta(seconds=duration),
        dtstart_local=local,
        tz_id=kw.pop("tz_id", "Europe/Berlin"),
        duration_s=duration,
        all_day=kw.pop("all_day", False),
        rrule=kw.pop("rrule", ""),
        rdate=kw.pop("rdate", ""),
        exdate=kw.pop("exdate", ""),
    )
    for key, value in kw.items():
        setattr(event, key, value)
    return event


WINDOW = (datetime(2026, 8, 1), datetime(2026, 12, 1))


def test_single_event_is_returned_once():
    got = list(instances(make(), *WINDOW))
    assert got == [(datetime(2026, 8, 24, 7, 30), datetime(2026, 8, 24, 8, 30))]


def test_event_that_started_before_the_window_still_overlaps_it():
    # The whole reason the query is "overlaps", not "starts within": a
    # three-week event that began last month is the most important thing on
    # today's screen.
    event = make(start=datetime(2026, 7, 20), local=datetime(2026, 7, 20), duration_s=20 * 86400)
    got = list(instances(event, *WINDOW))
    assert len(got) == 1
    assert got[0][0] == datetime(2026, 7, 20)


def test_weekly_rule_keeps_its_wall_clock_across_a_dst_change():
    # 09:30 Berlin is 07:30 UTC in summer and 08:30 UTC in winter. Expanding in
    # UTC would walk the meeting an hour; expanding in the zone does not.
    event = make(rrule="FREQ=WEEKLY;BYDAY=MO")
    got = list(instances(event, datetime(2026, 8, 1), datetime(2026, 11, 15)))
    summer = [s for s, _ in got if s.month == 8]
    winter = [s for s, _ in got if s.month == 11]
    assert all(s.hour == 7 and s.minute == 30 for s in summer)
    assert all(s.hour == 8 and s.minute == 30 for s in winter)


def test_exdate_removes_an_instance():
    event = make(rrule="FREQ=DAILY;COUNT=5", exdate="2026-08-26T09:30:00")
    starts = [s.date().day for s, _ in instances(event, *WINDOW)]
    assert starts == [24, 25, 27, 28]


def test_an_overridden_instance_is_skipped_by_the_master():
    # The override is its own row and writes its own occurrence. If the master
    # kept the slot, the day would show the meeting twice.
    event = make(rrule="FREQ=DAILY;COUNT=3")
    skip = {recurrence_key(datetime(2026, 8, 25, 9, 30), False)}
    starts = [s.date().day for s, _ in instances(event, *WINDOW, skip)]
    assert starts == [24, 26]


def test_all_day_events_are_dates_and_are_not_converted():
    event = make(
        start=datetime(2026, 8, 24),
        local=datetime(2026, 8, 24),
        duration_s=3 * 86400,
        all_day=True,
        tz_id="UTC",
        rrule="FREQ=YEARLY;COUNT=2",
    )
    got = list(instances(event, datetime(2026, 1, 1), datetime(2028, 1, 1)))
    assert got[0] == (datetime(2026, 8, 24), datetime(2026, 8, 27))
    assert got[1][0] == datetime(2027, 8, 24)


def test_an_unreadable_rule_still_shows_the_first_instance():
    event = make(rrule="FREQ=NONSENSE;BY=WHAT")
    got = list(instances(event, *WINDOW))
    assert got == [(datetime(2026, 8, 24, 7, 30), datetime(2026, 8, 24, 8, 30))]
