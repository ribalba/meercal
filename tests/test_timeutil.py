from datetime import date, datetime

import core.timeutil as timeutil
from core.timeutil import (
    days_touched,
    display_zone,
    from_utc,
    system_zone_name,
    to_utc,
    week_start_of,
    zone,
)


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


def test_the_zone_name_is_looked_for_in_all_three_places(monkeypatch, tmp_path):
    """TZ, then /etc/timezone, then the /etc/localtime symlink.

    The order is the point. A container has no zone of its own -- its
    /etc/localtime is UTC -- so the environment has to be able to say, and
    docker-compose.yml passes the host's zone in. /etc/timezone comes next
    because it holds the *name*: bind-mounting /etc/localtime into a container
    copies the zone data and leaves the name behind, which is exactly the case
    that used to end up in UTC with nothing to show for it.
    """
    name = tmp_path / "timezone"
    link = tmp_path / "localtime"
    monkeypatch.setattr(timeutil, "TZ_NAME_FILE", str(name))
    monkeypatch.setattr(timeutil, "TZ_LINK_FILE", str(link))

    monkeypatch.setenv("TZ", "Europe/Berlin")
    name.write_text("America/New_York\n")
    assert system_zone_name() == "Europe/Berlin"

    monkeypatch.delenv("TZ")
    assert system_zone_name() == "America/New_York"

    name.unlink()
    (tmp_path / "zoneinfo" / "Asia").mkdir(parents=True)
    (tmp_path / "zoneinfo" / "Asia" / "Tokyo").write_bytes(b"TZif")
    link.symlink_to(tmp_path / "zoneinfo" / "Asia" / "Tokyo")
    assert system_zone_name() == "Asia/Tokyo"

    link.unlink()
    # Nothing knows: UTC, which is a guess and not a fact -- see the note the
    # UI shows when the browser disagrees with it.
    assert system_zone_name() == "UTC"


def test_system_means_this_machine_and_not_the_container_it_runs_in(monkeypatch):
    # The bug this pair of files exists to prevent: `timezone = "system"` on a
    # server whose own zone is UTC drew every timed event two hours early, and
    # a calendar that is wrong by a constant still looks exactly like a
    # calendar.
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert display_zone("system") == zone("Europe/Berlin")
    assert from_utc(datetime(2026, 8, 25, 12, 0), display_zone("system")) == datetime(2026, 8, 25, 14, 0)
