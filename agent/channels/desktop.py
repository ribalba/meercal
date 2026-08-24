"""A notification on this machine, through ``notify-send``.

A subprocess rather than D-Bus directly. Speaking to
``org.freedesktop.Notifications`` properly would mean adding ``jeepney`` or
``dbus-next`` to a project that currently has no such dependency, and the whole
saving is one fork per reminder, which at a handful of reminders a day is
not a saving.

The flag that matters is ``--urgency=critical``. On both Plasma and GNOME it
means the notification does not expire on its own: it sits there until it is
dismissed. A reminder that fades after four seconds while you are looking at
another screen has reminded nobody, and the default urgency does exactly that.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .base import Channel, ChannelError, Notification

# Where the popup comes from, as far as the desktop is concerned. The
# desktop-entry hint is what makes Plasma and GNOME show meercal's own icon and
# group the notifications under its name rather than under "notify-send".
APP_NAME = "meercal"
DESKTOP_ENTRY = "meercal"

# The session bus is not optional and its absence is silent, which is the
# nastiest failure this channel has: under a systemd *system* unit there is no
# DBUS_SESSION_BUS_ADDRESS, notify-send exits non-zero into a log nobody reads,
# and every desktop reminder simply never appears.
BUS_VAR = "DBUS_SESSION_BUS_ADDRESS"


class DesktopSender(Channel):
    def available(self) -> bool:
        # False inside a container, under a systemd system unit, or anywhere
        # else without a session. meercal's own compose file offers to run the
        # agent in a container, and there this is the whole point: the queue is
        # shared, so a container that claimed these rows would swallow the
        # notifications meant for the desktop that could actually show them.
        return bool(shutil.which("notify-send")) and _has_session_bus()

    def send(self, note: Notification) -> None:
        binary = shutil.which("notify-send")
        if not binary:
            raise ChannelError("notify-send is not installed (package: libnotify)", permanent=True)
        if not _has_session_bus():
            raise ChannelError(_no_bus_message(), permanent=True)

        cfg = self.config
        argv = [
            binary,
            "--app-name", APP_NAME,
            "--urgency", cfg.urgency,
            "--hint", f"string:desktop-entry:{DESKTOP_ENTRY}",
            # Deduplicate in the daemon as well as in the queue: a reminder
            # re-sent after a retry replaces its own popup instead of stacking
            # a second one beside it.
            "--hint", f"string:x-canonical-private-synchronous:meercal-{note.channel}",
        ]
        if cfg.icon:
            argv += ["--icon", cfg.icon]
        # Ignored by most daemons at critical urgency, which is the point of
        # critical, but honoured for the other two, so it is worth sending.
        if cfg.expire:
            argv += ["--expire-time", str(cfg.expire)]
        argv += [note.title, note.body]

        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            # --wait is not passed, so this means the daemon is wedged.
            raise ChannelError("notify-send did not return; is the notification daemon up?")
        if result.returncode != 0:
            raise ChannelError(
                f"notify-send exited {result.returncode}: {(result.stderr or '').strip()}"
            )
        _play(cfg.sound)

    def check(self) -> str:
        if not shutil.which("notify-send"):
            raise ChannelError("notify-send is not installed (package: libnotify)")
        if not _has_session_bus():
            raise ChannelError(_no_bus_message())
        self.send(self.test_notification())
        return f"notify-send, urgency={self.config.urgency}, on {os.environ.get('XDG_CURRENT_DESKTOP', 'this desktop')}"


def _has_session_bus() -> bool:
    if os.environ.get(BUS_VAR):
        return True
    # systemd --user sets the variable; a plain login shell may instead only
    # have the socket where the address would point.
    uid = os.getuid() if hasattr(os, "getuid") else None
    return uid is not None and os.path.exists(f"/run/user/{uid}/bus")


def _no_bus_message() -> str:
    return (
        f"no session bus ({BUS_VAR} is unset), so no notification can be shown. "
        "Run the agent as a systemd --user service rather than a system one: "
        "see contrib/meercal-agent.service"
    )


def _play(sound: str) -> None:
    """Best effort, and never a delivery failure.

    The notification is the reminder; the sound is decoration. A machine with
    no audio must not turn a delivered popup into a failed one that gets
    retried three more times.
    """
    if not sound:
        return
    if os.path.sep in sound:
        argv = ["paplay", sound]
    else:
        argv = ["canberra-gtk-play", "-i", sound]
    if not shutil.which(argv[0]):
        return
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
