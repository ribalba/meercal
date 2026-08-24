from datetime import date, datetime

from core.timeutil import days_touched, from_utc, to_utc, week_start_of, zone


def test_days_touched_counts_dates_not_hours():
    # 09:00 to 24:00 is one day, not two: the interval ends exactly where the
    # next day starts, and the next day belongs to whatever begins in it.
    assert days_touched(datetime(2026, 3, 2, 9), datetime(2026, 3, 3)) == 1
    assert days_touched(datetime(2026, 3, 2, 9), datetime(2026, 3, 3, 0, 1)) == 2
    assert days_touched(datetime(2026, 3, 2), datetime(2026, 3, 21)) == 19


def test_days_touched_survives_a_dst_boundary():
    # 26 hours across the autumn change is still two days, which a
    # milliseconds-divided-by-86400000 count gets wrong.
    start = datetime(2026, 10, 24, 23)
    assert days_touched(start, datetime(2026, 10, 26, 1)) == 3


def test_week_start_of():
    monday = week_start_of(date(2026, 8, 27), 1)   # a Thursday
    assert monday == date(2026, 8, 24)
    sunday = week_start_of(date(2026, 8, 27), 7)
    assert sunday == date(2026, 8, 23)


def test_wall_time_round_trips_through_utc():
    wall = datetime(2026, 8, 24, 9, 30)
    utc = to_utc(wall, "Europe/Berlin")
    assert utc == datetime(2026, 8, 24, 7, 30)     # CEST, +2
    assert from_utc(utc, zone("Europe/Berlin")) == wall

    winter = datetime(2026, 1, 24, 9, 30)
    assert to_utc(winter, "Europe/Berlin") == datetime(2026, 1, 24, 8, 30)  # CET, +1


def test_unknown_zone_falls_back_to_utc_rather_than_raising():
    # Servers do send zone names this host has never heard of. One of them must
    # not cost the whole calendar.
    assert to_utc(datetime(2026, 8, 24, 9), "Mars/Olympus") == datetime(2026, 8, 24, 9)
