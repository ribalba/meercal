"""Configuration for the whole system: web app and agent, one file.

Everything lives in a single ``meercal.toml``, and every setting in it can be
overridden by an environment variable of the same name. Precedence, highest
first::

    constructor arg  >  environment  >  .env  >  meercal.toml  >  default

The file is optional. A server handed DATABASE_URL and SECRET_KEY in its
environment runs with no file at all. That is the remote deployment, where the
compose file simply drops the bind mount. The agent needs one, because
``[[agent.account]]`` is the only place calendar credentials are configured.

Path resolution: ``$MEERCAL_CONFIG``, else ``meercal.toml`` at the repository
root (``/app/meercal.toml`` in both images). Setting ``MEERCAL_CONFIG`` to the
empty string means "environment only" and skips both files, which is how the
test suite keeps a developer's own configuration out of a test run.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from . import timeutil

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
    ("server", "update_check"): "update_check",
    ("server", "places"): "places",
    ("meerail", "database_url"): "meerail_database_url",
    ("agent", "interval"): "agent_interval",
    ("reminders", "enabled"): "reminders_enabled",
    ("reminders", "tick"): "reminders_tick",
    ("reminders", "grace"): "reminders_grace",
    ("reminders", "quiet_hours"): "reminders_quiet_hours",
    ("reminders", "all_day_at"): "reminders_all_day_at",
    ("reminders", "host"): "reminders_host",
    ("reminders", "retention_days"): "reminders_retention_days",
}

# What each channel kind can do, for the one-line summary `--test` prints and
# for the error when a rule names a channel that is not configured.
CHANNEL_KINDS = {
    "desktop": "a notification on this machine, via notify-send",
    "ntfy":    "a push to a phone, via an ntfy topic",
    "twilio":  "a phone call (or an SMS), via Twilio",
    "command": "runs a program of your choosing",
    "webhook": "an HTTP POST of your choosing",
    "app":     "a notification in the meercal window, while it is open",
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
    # default: a calendar you never see costs one row and a sync token.
    only: str = ""
    # Google Calendar API only. Basic auth to Google's CalDAV endpoint has been
    # off for years, so this path is OAuth2 or nothing: create a Desktop client
    # in Google Cloud Console, then `meercal.sh google-auth` (or, from a
    # checkout, `python -m agent.google_auth`) to mint the refresh token.
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
        """Where discovery starts: the configured URL, else the kind's default."""
        return self.url or ACCOUNT_KINDS.get(self.kind, "")

    @property
    def name(self) -> str:
        return self.label or self.username or self.kind


# --- Reminders -------------------------------------------------------------
#
# A channel is *where* a reminder goes; a rule is *which* events get one and
# *when*. They are separate because the same channel is wanted by several rules
# and the same rule usually wants two channels, and because the thing holding
# a Twilio token should be named once.
#
# Each kind is its own model behind a discriminated union rather than one model
# with every field on it. It costs a few lines and buys the error message: a
# `topic` on a twilio channel is a startup failure that says so, instead of a
# call that never mentions where it was supposed to go.


class ChannelBase(BaseModel):
    """What every channel has, whatever it sends over."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Filled in from the table's own key: [reminders.channel.phone] -> "phone".
    name: str = ""
    enabled: bool = True

    # Which dispatcher may deliver this. Empty means any of them: right for
    # ntfy and Twilio, wrong for a desktop popup, which has to happen on the
    # machine you are sitting at. See `reminders.host`.
    host: str = ""

    # How much of the event leaves this machine. `full` is title, time and
    # place; `title` drops the place and the notes; `none` says only that there
    # is a reminder, and you open the app to see what it was. The setting
    # exists because a public ntfy topic is readable by anyone who guesses it,
    # and a calendar holds appointments you would not post publicly.
    detail: Literal["full", "title", "none"] = "full"

    # A hard stop, counted from the delivery table. A runaway rule against a
    # per-minute recurrence should cost a log line, not a phone bill.
    max_per_day: int = 0                # 0 = no cap

    # Quiet hours are meercal's own policy, not the desktop's: a `critical`
    # notification bypasses Plasma's Do Not Disturb by design, which is exactly
    # the urgency a reminder wants, so the rule has to be held here.
    ignore_quiet_hours: bool = False

    # How late this channel may still fire, overriding [reminders] grace. A
    # desktop popup about a meeting that started ten minutes ago is useful; a
    # phone call about one is worse than silence.
    grace: int = 0                      # 0 = use the global grace

    title: str = "{summary}"
    body: str = "{when}{calendar_suffix}{location_suffix}"

    def secret(self, value: str, env_name: str) -> str:
        """A credential, from the file or from the environment.

        The environment is preferred so that a token need not be written into a
        file that ``docker-compose.yml`` bind-mounts into the web container.
        """
        if env_name:
            return os.environ.get(env_name, "").strip()
        return value


class DesktopChannel(ChannelBase):
    """A notification on this machine, through ``notify-send``.

    A subprocess rather than D-Bus directly: speaking to
    ``org.freedesktop.Notifications`` would add a dbus library to a project
    that has none, to save one fork per reminder.
    """

    kind: Literal["desktop"]
    # `critical` is the one that matters: on both Plasma and GNOME it means the
    # notification does not expire on its own. A reminder that fades after four
    # seconds while you are looking at another screen has reminded nobody.
    urgency: Literal["low", "normal", "critical"] = "critical"
    # A canberra event name (`message`, `bell`), a path to a sound file, or ""
    # for silence.
    sound: str = "message"
    icon: str = ""
    # Milliseconds; ignored by most daemons when urgency is critical.
    expire: int = 0


class NtfyChannel(ChannelBase):
    """A push to a phone, over ntfy."""

    kind: Literal["ntfy"]
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""
    token_env: str = ""
    username: str = ""
    password: str = ""
    password_env: str = ""
    priority: int = 4
    tags: list[str] = ["calendar"]
    # Where tapping the notification goes. Only useful if the phone can reach
    # it, which for a localhost-only meercal it cannot.
    click: str = ""

    @property
    def auth_token(self) -> str:
        return self.secret(self.token, self.token_env)

    @property
    def auth_password(self) -> str:
        return self.secret(self.password, self.password_env)


class TwilioChannel(ChannelBase):
    """A phone call, or an SMS, over Twilio's REST API.

    The call is placed with inline TwiML rather than a ``Url``, which is the
    detail that makes this practical at all: no publicly reachable webhook, no
    tunnel, no second service. One authenticated POST.
    """

    kind: Literal["twilio"]
    account_sid: str = ""
    auth_token: str = ""
    auth_token_env: str = ""
    # `from` is a keyword, so the field is `from_` and the TOML key is `from`.
    from_: str = Field("", alias="from")
    to: str = ""
    mode: Literal["call", "sms"] = "call"
    say: str = "{summary}. {when}."
    voice: str = "Polly.Vicki"
    language: str = "de-DE"
    # Said twice by default: a sentence heard once, half asleep, is a sentence
    # not heard.
    repeat: int = 2
    ring_seconds: int = 30
    # A call about a meeting that ended an hour ago is worse than no call, so
    # this one defaults to a much shorter grace than the rest of the system.
    grace: int = 300

    @property
    def token(self) -> str:
        return self.secret(self.auth_token, self.auth_token_env)


class CommandChannel(ChannelBase):
    """Runs a program. The escape hatch that stops every future request from
    being a code change: a smart bulb, ``signal-cli``, an alarm sound."""

    kind: Literal["command"]
    # The reminder's fields arrive in the environment as MEERCAL_SUMMARY,
    # MEERCAL_START, MEERCAL_WHEN, MEERCAL_CALENDAR, MEERCAL_LOCATION.
    argv: list[str] = []
    timeout: int = 20


class WebhookChannel(ChannelBase):
    """An HTTP POST of the reminder as JSON, anywhere."""

    kind: Literal["webhook"]
    url: str = ""
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = {}
    token: str = ""
    token_env: str = ""

    @property
    def auth_token(self) -> str:
        return self.secret(self.token, self.token_env)


class AppChannel(ChannelBase):
    """A notification raised by the meercal window itself.

    Claimed by the browser rather than by the agent, from the same queue as
    everything else, so it composes with the rest instead of being a second,
    parallel way for a reminder to happen. It is also the only channel that
    works on a machine where the agent is not running.
    """

    kind: Literal["app"]


ReminderChannel = Annotated[
    Union[
        DesktopChannel,
        NtfyChannel,
        TwilioChannel,
        CommandChannel,
        WebhookChannel,
        AppChannel,
    ],
    Field(discriminator="kind"),
]


class ReminderRule(BaseModel):
    """Which events get reminded about, how long before, and on what.

    ``match`` is a filter string in exactly the language the filter bar takes,
    ``cal:work is:busy with:anna``, because inventing a second, worse query
    language for the config file would be the wrong kind of new.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = ""
    match: str = ""
    # Subtracted from the match. The query language has no negation operator
    # and does not need one for this: two queries and a set difference.
    except_: str = Field("", alias="except")
    regex: bool = False
    # A hidden calendar is still synced and still searched, and hiding is about
    # drawing rather than about data, so by default it still reminds. A rule
    # that disagrees says so.
    visible_only: bool = False

    # "10m", ["1h", "10m"], or "valarm" to take the times from the event's own
    # VALARM. Mutually exclusive with `at`.
    lead: Union[str, list[str]] = ""
    # "-1d 18:00": an absolute wall-clock anchor, for all-day events where
    # "ten minutes before" means nothing.
    at: str = ""

    channels: list[str] = []

    @field_validator("lead")
    @classmethod
    def _leads(cls, v):
        for item in ([v] if isinstance(v, str) else v):
            if not item or item == "valarm":
                continue
            timeutil.parse_duration(item)   # raises with a useful message
        return v

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        if v:
            timeutil.parse_at(v)
        return v

    @model_validator(mode="after")
    def _one_timing(self):
        if self.lead and self.at:
            raise ValueError(
                f"reminder rule {self.name or '(unnamed)'!r}: set `lead` or `at`, not both"
            )
        if not self.lead and not self.at:
            raise ValueError(
                f"reminder rule {self.name or '(unnamed)'!r}: needs a `lead` (e.g. \"10m\") "
                f"or an `at` (e.g. \"-1d 18:00\")"
            )
        if not self.channels:
            raise ValueError(
                f"reminder rule {self.name or '(unnamed)'!r}: needs at least one channel"
            )
        return self

    @property
    def leads(self) -> list[str]:
        """The lead times, always as a list. Empty when the rule uses ``at``."""
        if not self.lead:
            return []
        return [self.lead] if isinstance(self.lead, str) else [x for x in self.lead if x]


def _refuse_orphan_account_keys(agent: Any) -> None:
    """Account keys sitting in ``[agent]`` because the header stayed commented.

    Both the written file and the example ship the block commented out, header
    and all. Filling in the fields without also uncommenting
    ``[[agent.account]]`` leaves the credentials as keys of ``[agent]``, where
    nothing reads them: the file looks full of an account and the agent reports
    none at all. That is the same silence a mistyped key inside a block gets
    refused for, so it is refused here too.
    """
    if not isinstance(agent, dict):
        return
    stray = sorted(k for k in agent if k in AccountConfig.model_fields)
    if not stray:
        return
    raise ValueError(
        f"[agent] has calendar account keys directly in it: {', '.join(stray)}.\n"
        "\n"
        "Those belong under a [[agent.account]] header, and the one above them "
        "is still commented out, so they configure nothing and the agent sees "
        "no accounts at all. Uncomment the [[agent.account]] line."
    )


class TomlSource(PydanticBaseSettingsSource):
    """meercal.toml, flattened onto the field names above.

    Unknown sections and unknown keys are ignored rather than rejected: this
    same file is read by two processes that each only care about half of it,
    and a future version's key must not stop an older binary from starting.
    The exception is a key whose mistake this can name back; see
    ``_refuse_orphan_account_keys``.
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
        _refuse_orphan_account_keys(raw.get("agent"))

        # [reminders.channel.<name>] and [[reminders.rule]] are nested deeper
        # than the flat map above can reach, the same way [[agent.account]] is.
        reminders = raw.get("reminders")
        if isinstance(reminders, dict):
            channels = reminders.get("channel")
            if isinstance(channels, dict):
                # The table's key is the channel's name: it is what a rule
                # refers to, so it belongs on the object rather than only in
                # the mapping around it.
                out["reminder_channels"] = {
                    name: {**cfg, "name": name}
                    for name, cfg in channels.items()
                    if isinstance(cfg, dict)
                }
            rules = reminders.get("rule")
            if isinstance(rules, list):
                out["reminder_rules"] = rules
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

    # Whether the server may ask github once a day whether a newer release is
    # out. It is the only outbound request this program makes, and on a machine
    # holding years of somebody's time that is worth being a setting rather
    # than a default nobody was told about. See app/updates.py.
    update_check: bool = True

    # Where you keep going. name -> what goes in the field, offered as chips
    # under the event panel's Where box. A dict rather than a list because the
    # name is the label and the value is the text, and TOML keeps the order it
    # was written in, which is the order they will be offered in.
    places: dict[str, str] = {}

    # --- the rest of the suite ---
    meerail_database_url: str = ""

    # --- agent ---
    agent_interval: int = 300
    accounts: list[AccountConfig] = []

    # --- reminders ---
    # The scheduler runs in the agent, because the agent is the half that holds
    # credentials and the half that has a session bus. With no rules configured
    # the loop is never started, so `enabled` is a kill switch rather than the
    # thing that turns the feature on.
    reminders_enabled: bool = True
    reminders_tick: int = 30
    # How late a reminder may still fire. A laptop suspended at 08:40 and opened
    # at 09:40 has an armed 09:00 reminder in the queue: inside this window it
    # fires and says it is late, outside it it is recorded as missed rather than
    # disappearing.
    reminders_grace: int = 600
    reminders_quiet_hours: str = ""
    # Where "before" starts for an event that has no clock on it. A birthday
    # cannot be led away from midnight without waking you.
    reminders_all_day_at: str = "09:00"
    # This dispatcher's name, matched against a channel's `host`. Empty means
    # this host's own name.
    reminders_host: str = ""
    reminders_retention_days: int = 30
    reminder_channels: dict[str, ReminderChannel] = {}
    reminder_rules: list[ReminderRule] = []

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

    @field_validator("reminders_quiet_hours")
    @classmethod
    def _quiet(cls, v: str) -> str:
        timeutil.parse_quiet_hours(v)
        return v

    @field_validator("reminders_all_day_at")
    @classmethod
    def _all_day_at(cls, v: str) -> str:
        timeutil.parse_clock(v)
        return v

    @model_validator(mode="after")
    def _rules_name_real_channels(self):
        """A rule pointing at a channel that does not exist is silence.

        And silence is the one failure this whole subsystem cannot report on
        its own, so it is worth refusing to start over, the same way a
        mistyped account key is.
        """
        known = set(self.reminder_channels)
        for rule in self.reminder_rules:
            missing = [c for c in rule.channels if c not in known]
            if missing:
                have = ", ".join(sorted(known)) or "(none configured)"
                raise ValueError(
                    f"reminder rule {rule.name or '(unnamed)'!r} sends to unknown "
                    f"channel(s) {', '.join(missing)}; configured channels: {have}"
                )
        return self

    @property
    def host_name(self) -> str:
        """This dispatcher's name. Configured, else the machine's own."""
        if self.reminders_host:
            return self.reminders_host
        import socket

        return socket.gethostname()

    @property
    def quiet_window(self) -> tuple[int, int] | None:
        return timeutil.parse_quiet_hours(self.reminders_quiet_hours)

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
    # ValidationError is a ValueError, and TomlSource raises plain ones for the
    # mistakes it can name better than pydantic can. Either way it is a broken
    # config, which should say so rather than traceback.
    except ValueError as exc:
        path = config_path()
        where = f" in {path}" if path else ""
        raise SystemExit(f"meercal: bad configuration{where}\n\n{exc}") from exc
