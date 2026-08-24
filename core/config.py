"""Configuration for the whole system — web app and agent, one file.

Everything lives in a single ``meercal.toml``, and every setting in it can be
overridden by an environment variable of the same name. Precedence, highest
first::

    constructor arg  >  environment  >  .env  >  meercal.toml  >  default

The file is optional. A server handed DATABASE_URL and SECRET_KEY in its
environment runs with no file at all — that is the remote deployment, where the
compose file simply drops the bind mount. The agent needs one, because
``[[agent.account]]`` is the only place calendar credentials are configured.

Path resolution: ``$MEERCAL_CONFIG``, else ``meercal.toml`` at the repository
root (``/app/meercal.toml`` in both images). Setting ``MEERCAL_CONFIG`` to the
empty string means "environment only" and skips both files — which is how the
test suite keeps a developer's own configuration out of a test run.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "meercal.toml"
EXAMPLE_CONFIG_PATH = BASE_DIR / "meercal.example.toml"

# What each account kind means on the wire. The URL is only a default: a kind
# exists so that the common cases need one word instead of a service discovery
# URL nobody remembers.
#
# iCloud hands every account a *personal* host (p42-caldav.icloud.com and
# friends) in the PROPFIND response, so this is a bootstrap address and not
# where the requests end up. That redirect is normal and the agent follows it.
ACCOUNT_KINDS = {
    "icloud": "https://caldav.icloud.com",
    "fastmail": "https://caldav.fastmail.com/dav/",
    "caldav": "",   # generic: url is required
    "ics": "",      # a single read-only .ics feed; url is the feed
    "google": "",   # Google Calendar API v3 over OAuth2; url unused
}

# Section/key in the TOML -> field name on Settings. The environment variable
# for a field is its own name upper-cased, which is what makes DATABASE_URL and
# SERVER_PASSWORD line up with `database.url` and `server.password`.
_FIELD_MAP: dict[tuple[str, str], str] = {
    ("database", "url"): "database_url",
    ("server", "secret_key"): "secret_key",
    ("server", "password"): "server_password",
    ("server", "timezone"): "timezone",
    ("server", "week_start"): "week_start",
    ("server", "day_start"): "day_start",
    ("server", "day_end"): "day_end",
    ("server", "default_view"): "default_view",
    ("server", "horizon_past_days"): "horizon_past_days",
    ("server", "horizon_future_days"): "horizon_future_days",
    ("server", "trusted_proxies"): "trusted_proxies",
    ("server", "places"): "places",
    ("meerail", "database_url"): "meerail_database_url",
    ("agent", "interval"): "agent_interval",
}


class AccountConfig(BaseModel):
    """One calendar account: where it is, who you are on it, what to sync."""

    # A mistyped key here used to be silently dropped, which is the whole class
    # of bug this file exists to end: reject it instead.
    model_config = ConfigDict(extra="forbid")

    label: str = ""
    kind: str = "caldav"
    url: str = ""
    username: str = ""
    password: str = ""
    # Regex over the calendar's display name. Unset syncs everything the
    # account offers and leaves the choosing to the UI, which is the better
    # default — a calendar you never see costs one row and a sync token.
    only: str = ""
    # Google Calendar API only. Basic auth to Google's CalDAV endpoint has been
    # off for years, so this path is OAuth2 or nothing: create a Desktop client
    # in Google Cloud Console and run `python -m agent.google_auth` to mint the
    # refresh token.
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        v = (v or "caldav").strip().lower()
        if v not in ACCOUNT_KINDS:
            raise ValueError(f"unknown account kind {v!r}; one of {sorted(ACCOUNT_KINDS)}")
        return v

    @property
    def base_url(self) -> str:
        """Where discovery starts — the configured URL, else the kind's default."""
        return self.url or ACCOUNT_KINDS.get(self.kind, "")

    @property
    def name(self) -> str:
        return self.label or self.username or self.kind


class TomlSource(PydanticBaseSettingsSource):
    """meercal.toml, flattened onto the field names above.

    Unknown sections and unknown keys are ignored rather than rejected: this
    same file is read by two processes that each only care about half of it,
    and a future version's key must not stop an older binary from starting.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None):
        super().__init__(settings_cls)
        self._data = self._load(path)

    @staticmethod
    def _load(path: Path | None) -> dict[str, Any]:
        if path is None or not path.is_file():
            return {}
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        out: dict[str, Any] = {}
        for (section, key), field in _FIELD_MAP.items():
            if section in raw and isinstance(raw[section], dict) and key in raw[section]:
                out[field] = raw[section][key]
        accounts = raw.get("agent", {}).get("account") if isinstance(raw.get("agent"), dict) else None
        if isinstance(accounts, list):
            out["accounts"] = accounts
        return out

    def get_field_value(self, field, field_name):  # noqa: D102 - pydantic hook
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- database ---
    database_url: str = "postgresql+psycopg://meercal:meercal@127.0.0.1:5433/meercal"

    # --- server ---
    secret_key: str = "dev-insecure-secret-change-me"
    server_password: str = ""
    timezone: str = "system"
    week_start: int = 1
    day_start: int = 8
    day_end: int = 20
    default_view: str = "ribbon"
    # Why materialise at all: see core/expand.py. These bound how much of the
    # infinite series of a weekly meeting actually exists as rows.
    horizon_past_days: int = 730
    horizon_future_days: int = 1095
    trusted_proxies: list[str] = []

    # Where you keep going. name -> what goes in the field, offered as chips
    # under the event panel's Where box. A dict rather than a list because the
    # name is the label and the value is the text, and TOML keeps the order it
    # was written in — which is the order they will be offered in.
    places: dict[str, str] = {}

    # --- the rest of the suite ---
    meerail_database_url: str = ""

    # --- agent ---
    agent_interval: int = 300
    accounts: list[AccountConfig] = []

    @field_validator("week_start")
    @classmethod
    def _week_start(cls, v: int) -> int:
        if v not in (1, 7):
            raise ValueError("week_start must be 1 (Monday) or 7 (Sunday)")
        return v

    @field_validator("default_view")
    @classmethod
    def _view(cls, v: str) -> str:
        v = (v or "ribbon").strip().lower()
        if v not in ("ribbon", "week", "month", "day"):
            raise ValueError(f"unknown default_view {v!r}")
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings, env_settings, dotenv_settings, TomlSource(settings_cls, config_path()))


def config_path() -> Path | None:
    """Which file to read, or None for "environment only".

    ``MEERCAL_CONFIG=""`` is not the same as unset: it is how a test run says
    "ignore whatever this developer has on their machine".
    """
    env = os.environ.get("MEERCAL_CONFIG")
    if env is not None:
        return Path(env) if env.strip() else None
    return DEFAULT_CONFIG_PATH


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # a broken config should say so, not traceback
        path = config_path()
        where = f" in {path}" if path else ""
        raise SystemExit(f"meercal: bad configuration{where}\n\n{exc}") from exc
