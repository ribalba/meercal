"""Where a reminder actually goes.

A channel is a class with two methods: :meth:`Channel.send`, which delivers
one notification, and :meth:`Channel.check`, which proves it could. Raising
from ``send`` puts the row back in the queue with one more attempt against it;
returning means it went. That is the whole contract, and adding a fifth kind is
one file and one line in :data:`REGISTRY`.

The split between this package and ``core/reminders.py`` is the same split the
rest of meercal has: policy decides *whether and when*, and lives where both
halves of the program can read it; delivery decides *how*, needs credentials,
and lives only in the agent.
"""

from __future__ import annotations

from .base import Channel, ChannelError, Notification
from .command import CommandSender, WebhookSender
from .desktop import DesktopSender
from .ntfy import NtfySender
from .twilio import TwilioSender

# kind -> the class that delivers it. `app` is deliberately absent: those rows
# are claimed by the browser, not by this process.
REGISTRY: dict[str, type[Channel]] = {
    "desktop": DesktopSender,
    "ntfy": NtfySender,
    "twilio": TwilioSender,
    "command": CommandSender,
    "webhook": WebhookSender,
}


def build(config) -> Channel | None:
    """The sender for one configured channel, or None if nothing sends it."""
    cls = REGISTRY.get(config.kind)
    return cls(config) if cls else None


__all__ = ["Channel", "ChannelError", "Notification", "REGISTRY", "build"]
