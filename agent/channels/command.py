"""The two escape hatches: run a program, or POST some JSON.

Between them they cover Home Assistant, a smart bulb, ``signal-cli``, a script
that flashes the keyboard, and whatever comes next, with no opinion from
meercal about any of it. Their existence is what stops every future "can it
also…" from being a change to this package.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import httpx

from .base import Channel, ChannelError, Notification

TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class CommandSender(Channel):
    """Runs an argv, with the reminder in the environment.

    The environment rather than argv substitution, because a meeting titled
    ``"; rm -rf ~"`` is a thing somebody will eventually put in a calendar you
    subscribe to, and an environment variable cannot become an argument.
    """

    def send(self, note: Notification) -> None:
        argv = list(self.config.argv or [])
        if not argv:
            raise ChannelError("no argv configured", permanent=True)

        env = dict(os.environ)
        env.update(
            {
                "MEERCAL_TITLE": note.title,
                "MEERCAL_BODY": note.body,
                "MEERCAL_SUMMARY": str(note.fields.get("summary", "")),
                "MEERCAL_WHEN": str(note.fields.get("when", "")),
                "MEERCAL_CALENDAR": str(note.fields.get("calendar", "")),
                "MEERCAL_LOCATION": str(note.fields.get("location", "")),
                "MEERCAL_START": note.start_utc.isoformat(),
                "MEERCAL_ALL_DAY": "1" if note.all_day else "0",
                "MEERCAL_LATE": "1" if note.late else "0",
            }
        )
        try:
            result = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=self.config.timeout
            )
        except FileNotFoundError:
            raise ChannelError(f"{argv[0]!r} not found", permanent=True) from None
        except subprocess.TimeoutExpired:
            raise ChannelError(f"{argv[0]!r} did not finish in {self.config.timeout}s")
        if result.returncode != 0:
            raise ChannelError(
                f"{argv[0]!r} exited {result.returncode}: {(result.stderr or '').strip()[:200]}"
            )

    def check(self) -> str:
        argv = list(self.config.argv or [])
        if not argv:
            raise ChannelError("no argv configured")
        if not shutil.which(argv[0]) and not os.path.exists(argv[0]):
            raise ChannelError(f"{argv[0]!r} not found on PATH")
        self.send(self.test_notification())
        return " ".join(argv)


class WebhookSender(Channel):
    """POSTs the reminder as JSON."""

    def send(self, note: Notification) -> None:
        cfg = self.config
        if not cfg.url:
            raise ChannelError("no url configured", permanent=True)
        headers = dict(cfg.headers or {})
        if cfg.auth_token:
            headers.setdefault("Authorization", f"Bearer {cfg.auth_token}")
        payload = {
            "title": note.title,
            "body": note.body,
            "channel": note.channel,
            "start": note.start_utc.isoformat(),
            "fire_at": note.fire_at.isoformat(),
            "all_day": note.all_day,
            "late": note.late,
            **note.fields,
        }
        try:
            response = httpx.request(
                cfg.method, cfg.url, json=payload, headers=headers, timeout=TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise ChannelError(
                f"{cfg.url} returned {response.status_code}: {response.text[:200]}",
                permanent=response.status_code in (401, 403, 404),
            )

    def check(self) -> str:
        if not self.config.url:
            raise ChannelError("no url configured")
        if self.config.token_env and not self.config.auth_token:
            raise ChannelError(f"{self.config.token_env} is not set in the environment")
        self.send(self.test_notification())
        return f"{self.config.method} {self.config.url}"
