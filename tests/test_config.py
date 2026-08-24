"""The configuration loader: the file, the environment, and what wins.

Worth its own tests because it is the one piece both processes depend on and
the one where a mistake is silent — a mistyped key that is quietly ignored is
how a calendar ends up not syncing with nothing in the log to say why.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.config import Settings, config_path


@pytest.fixture()
def toml(tmp_path, monkeypatch):
    """Write a config file and point the loader at it.

    The environment is cleared of the settings these tests are about, because
    it *beats* the file — conftest exports DATABASE_URL for the API tests, and
    a file test that silently read that instead would be testing nothing.
    """
    for name in ("DATABASE_URL", "SERVER_PASSWORD", "SECRET_KEY", "TIMEZONE"):
        monkeypatch.delenv(name, raising=False)

    def write(text: str) -> Path:
        path = tmp_path / "meercal.toml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("MEERCAL_CONFIG", str(path))
        return path
    return write


def load() -> Settings:
    # Settings() rather than get_settings(): the latter is cached for the life
    # of the process, which is right in an app and wrong in a test.
    return Settings()


def test_defaults_stand_with_no_file_at_all(monkeypatch):
    monkeypatch.setenv("MEERCAL_CONFIG", "")
    settings = load()
    assert config_path() is None
    assert settings.server_password == ""
    assert settings.week_start == 1
    assert settings.places == {}
    assert settings.accounts == []


def test_sections_map_onto_fields(toml):
    toml("""
[database]
url = "postgresql+psycopg://u:p@db:5432/cal"

[server]
password = "hunter2"
week_start = 7
timezone = "Pacific/Auckland"
default_view = "month"
""")
    settings = load()
    assert settings.database_url.endswith("/cal")
    assert settings.server_password == "hunter2"
    assert settings.week_start == 7
    assert settings.timezone == "Pacific/Auckland"
    assert settings.default_view == "month"


def test_places_keep_the_order_they_were_written_in(toml):
    # They are offered as chips in this order, so it is part of the contract.
    toml("""
[server.places]
"Office" = "Ritterstr. 12"
"Meet" = "https://meet.example.com/x"
"Kita" = "Reichenberger Str. 1"
""")
    assert list(load().places) == ["Office", "Meet", "Kita"]
    assert load().places["Meet"] == "https://meet.example.com/x"


def test_the_environment_beats_the_file(toml, monkeypatch):
    toml('[server]\npassword = "from-file"\n')
    monkeypatch.setenv("SERVER_PASSWORD", "from-env")
    assert load().server_password == "from-env"


def test_accounts_are_read_as_a_list_of_blocks(toml):
    toml("""
[[agent.account]]
label = "Family"
kind = "icloud"
username = "you@icloud.com"
password = "app-specific"

[[agent.account]]
label = "Feed"
kind = "ics"
url = "https://example.com/holidays.ics"
""")
    accounts = load().accounts
    assert [a.label for a in accounts] == ["Family", "Feed"]
    # iCloud's URL comes from the kind; discovery moves it to a personal host.
    assert accounts[0].base_url == "https://caldav.icloud.com"
    assert accounts[1].base_url.endswith("holidays.ics")


def test_a_mistyped_account_key_is_rejected_rather_than_ignored(toml):
    toml("""
[[agent.account]]
label = "Family"
kind = "icloud"
passwrod = "typo"
""")
    with pytest.raises(Exception) as exc:
        load()
    assert "passwrod" in str(exc.value)


def test_an_unknown_kind_is_rejected(toml):
    toml('[[agent.account]]\nlabel = "X"\nkind = "carrier-pigeon"\n')
    with pytest.raises(Exception):
        load()


def test_a_future_key_does_not_stop_an_older_binary(toml):
    # The same file is read by two processes that each care about half of it,
    # and by whichever version of each is installed.
    toml('[server]\npassword = "x"\nsomething_from_next_year = true\n\n[nonsense]\nkey = 1\n')
    assert load().server_password == "x"
