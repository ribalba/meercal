"""A push to a phone, over ntfy.

One HTTP POST, with ``httpx``, already an agent dependency because CalDAV is
HTTP too. No SDK, no broker, no account.

The privacy note is not decoration. A public ntfy.sh topic is readable by
anyone who knows or guesses its name, and what travels over it is the titles of
your appointments: the doctor, the family, the interview. Two things follow,
and both are in the design rather than in a warning: make the topic long and
random rather than ``meercal-didi``, and let a channel say how much detail may
leave the machine (``detail = "full" | "title" | "none"``). Self-hosting ntfy
with authentication is the real answer, which is why ``server`` is a setting.
"""

from __future__ import annotations

import httpx

from .base import Channel, ChannelError, Notification

TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class NtfySender(Channel):
    def send(self, note: Notification) -> None:
        cfg = self.config
        if not cfg.topic:
            raise ChannelError("no topic configured", permanent=True)

        url = f"{cfg.server.rstrip('/')}/{cfg.topic.lstrip('/')}"
        headers = {
            "Title": _header_safe(note.title),
            "Priority": str(cfg.priority),
            "Markdown": "no",
        }
        if cfg.tags:
            headers["Tags"] = ",".join(cfg.tags)
        if cfg.click:
            headers["Click"] = cfg.click

        token = cfg.auth_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        auth = None
        if cfg.username:
            auth = (cfg.username, cfg.auth_password)

        try:
            response = httpx.post(
                url,
                content=(note.body or note.title).encode("utf-8"),
                headers=headers,
                auth=auth,
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (401, 403):
            raise ChannelError(
                f"ntfy refused the request ({response.status_code}); check the token "
                f"and whether {cfg.topic!r} needs one",
                permanent=True,
            )
        if response.status_code >= 400:
            raise ChannelError(f"ntfy returned {response.status_code}: {response.text[:200]}")

    def check(self) -> str:
        cfg = self.config
        if not cfg.topic:
            raise ChannelError("no topic configured")
        if cfg.token_env and not cfg.auth_token:
            raise ChannelError(f"{cfg.token_env} is not set in the environment")
        if cfg.server.startswith("https://ntfy.sh") and not cfg.auth_token:
            hint = " (public topic: anyone who guesses the name can read it)"
        else:
            hint = ""
        self.send(self.test_notification())
        return f"{cfg.server}/{cfg.topic}, detail={cfg.detail}{hint}"


def _header_safe(text: str) -> str:
    """ntfy carries the title in an HTTP header, which is Latin-1 and one line.

    An umlaut in a meeting title would otherwise raise inside httpx while
    encoding the request, and the notification would fail for a reason that has
    nothing to do with ntfy, on exactly the calendars this program is for.
    """
    flat = " ".join(str(text or "").split())
    try:
        flat.encode("latin-1")
        return flat
    except UnicodeEncodeError:
        # RFC 2047, which ntfy's clients decode.
        from base64 import b64encode

        return "=?UTF-8?B?" + b64encode(flat.encode("utf-8")).decode("ascii") + "?="
