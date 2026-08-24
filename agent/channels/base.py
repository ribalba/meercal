"""The contract every channel meets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class ChannelError(Exception):
    """Delivery failed. Retryable unless ``permanent`` is set.

    The distinction matters: a wrong Twilio token will still be wrong on the
    fourth attempt, and burning three retries on it only delays the moment the
    error becomes visible in the UI.
    """

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class Notification:
    """One thing to tell somebody.

    Built from the delivery row's stored payload, not from the event as it is
    now: an event renamed in the hour before it starts must not rewrite what
    you were told, and a retry has to say what the first attempt said.
    """

    title: str
    body: str
    fields: dict
    channel: str
    fire_at: datetime
    start_utc: datetime
    all_day: bool
    late: bool = False

    @property
    def text(self) -> str:
        """Title and body as one line, for channels with only one field."""
        return f"{self.title}: {self.body}" if self.body else self.title


class Channel:
    """Base class. Subclasses implement ``send``; ``check`` is optional."""

    def __init__(self, config):
        self.config = config
        self.name = config.name or config.kind

    def available(self) -> bool:
        """Can *this process* deliver on this channel at all?

        Not "is it configured correctly"; that is ``check``. This is the
        structural question, and it has exactly one interesting answer: a
        desktop notification cannot happen in a container, and a dispatcher
        that cannot deliver must not claim the row, or it takes the reminder
        away from the machine that could have shown it.
        """
        return True

    def send(self, note: Notification) -> None:
        raise NotImplementedError

    def check(self) -> str:
        """Prove this channel could deliver, and say what it found.

        Called by ``--test``. Raising means the channel is not usable, and the
        message is what the user will act on, so it says what is missing, not
        that something went wrong.
        """
        return "ok"

    def test_notification(self) -> Notification:
        from core.timeutil import utcnow

        now = utcnow()
        return Notification(
            title="meercal test",
            body=f"{self.name} is working · this is not a real reminder",
            fields={"summary": "meercal test", "when": "now"},
            channel=self.name,
            fire_at=now,
            start_utc=now,
            all_day=False,
        )
