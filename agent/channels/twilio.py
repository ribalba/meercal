"""A phone call, or an SMS, over Twilio's REST API.

The detail that makes this practical: Twilio's Calls endpoint accepts a
``Twiml`` parameter carrying the markup inline, as an alternative to a ``Url``
it would have to fetch. So there is **no publicly reachable webhook**: no
tunnel, no second service, no exposing meercal to the internet to be told about
a dentist appointment. It is one authenticated POST with ``httpx``, and the
Twilio SDK is not needed for it.

This is also the channel that costs money and wakes people, so it is the one
with a daily cap, a short grace, and a ``--test`` that is worth running before
any rule points at it.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

import httpx

from .base import Channel, ChannelError, Notification

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{resource}.json"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class TwilioSender(Channel):
    def send(self, note: Notification) -> None:
        cfg = self.config
        sid, token = cfg.account_sid, cfg.token
        if not (sid and token and cfg.from_ and cfg.to):
            raise ChannelError(
                "needs account_sid, auth_token (or auth_token_env), from and to",
                permanent=True,
            )

        if cfg.mode == "sms":
            resource, data = "Messages", {
                "To": cfg.to, "From": cfg.from_, "Body": note.text[:1500],
            }
        else:
            resource, data = "Calls", {
                "To": cfg.to,
                "From": cfg.from_,
                "Timeout": str(cfg.ring_seconds),
                "Twiml": _twiml(cfg, note),
            }

        try:
            response = httpx.post(
                API.format(sid=sid, resource=resource),
                data=data,
                auth=(sid, token),
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (401, 403):
            raise ChannelError(
                "Twilio rejected the credentials; check account_sid and the auth token",
                permanent=True,
            )
        if response.status_code >= 400:
            detail = _twilio_message(response)
            # 21xxx is Twilio's family for "your request was wrong": an
            # unverified number, a bad `from`, a malformed number. Retrying
            # those three more times only delays the error reaching the UI.
            raise ChannelError(f"Twilio: {detail}", permanent=response.status_code < 500)

    def check(self) -> str:
        cfg = self.config
        missing = [
            n for n, v in (
                ("account_sid", cfg.account_sid),
                ("auth_token", cfg.token),
                ("from", cfg.from_),
                ("to", cfg.to),
            ) if not v
        ]
        if missing:
            extra = ""
            if "auth_token" in missing and cfg.auth_token_env:
                extra = f" ({cfg.auth_token_env} is not set in the environment)"
            raise ChannelError(f"missing: {', '.join(missing)}{extra}")

        # Verify the credentials without placing a call: fetching the account
        # costs nothing and answers the only question `--test` is really
        # asking, which is whether the token works.
        try:
            response = httpx.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{cfg.account_sid}.json",
                auth=(cfg.account_sid, cfg.token),
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"cannot reach Twilio: {exc}") from exc
        if response.status_code >= 400:
            raise ChannelError(f"Twilio: {_twilio_message(response)}")

        name = response.json().get("friendly_name", cfg.account_sid)
        cap = f", max {cfg.max_per_day}/day" if cfg.max_per_day else ""
        return (
            f"{cfg.mode} {cfg.from_} -> {cfg.to} on {name}{cap} "
            f"(credentials verified; no {cfg.mode} placed)"
        )


def _twiml(cfg, note: Notification) -> str:
    """The whole call, as markup, in the request.

    A pause first, because the first second of a call is lost to the handset
    connecting and a reminder that starts talking into that is a reminder half
    heard. Said twice by default for the same reason.
    """
    from core.reminders import render

    text = render(cfg.say, {**note.fields, "when": note.fields.get("when", "")})
    said = escape(text)
    body = "".join(
        f'<Say voice="{escape(cfg.voice)}" language="{escape(cfg.language)}">{said}</Say>'
        f'<Pause length="1"/>'
        for _ in range(max(1, cfg.repeat))
    )
    return f'<Response><Pause length="1"/>{body}</Response>'


def _twilio_message(response) -> str:
    try:
        payload = response.json()
        return f"{payload.get('message', response.text[:200])} (code {payload.get('code')})"
    except ValueError:
        return f"{response.status_code}: {response.text[:200]}"
