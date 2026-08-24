"""Is a newer meercal out? The one outbound call the server ever makes.

It fetches the ``VERSION`` file from the repository's default branch and
compares it with the version this process is running (core/version.py). That
file is the release itself: the published images are tagged with exactly what
it contains, so "what main says" and "what `docker pull` would get you" cannot
drift apart the way a hand-maintained latest.json does.

Three properties matter more than the feature does:

  * **It never blocks a request.** The endpoint answers from the cache and
    kicks off a refresh in the background if that cache is stale, so the first
    page load after a restart says "no idea yet" for a second rather than
    holding the UI open against a network that may be firewalled. The banner
    appears on the next poll.
  * **It never fails loudly.** A blocked network, a proxy serving an HTML error
    page, a rate limit: all of it lands in ``error`` and the UI shows nothing.
    An update check is not worth an error state in a calendar.
  * **It can be switched off.** ``server.update_check = false`` in meercal.toml
    means this module makes no request at all, ever. It is the only thing in
    the server that talks to the internet, and on a machine holding years of
    somebody's time that deserves to be a decision rather than a default nobody
    was told about.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from core.config import get_settings
from core.version import VERSION, is_outdated

log = logging.getLogger(__name__)

# The default branch's VERSION file. Raw githubusercontent, not the API: no
# rate limit worth worrying about, no token, and the response is ~6 bytes.
LATEST_URL = "https://raw.githubusercontent.com/ribalba/meercal/main/VERSION"

# Where the UI sends someone who has just been told an update exists. Not the
# releases page: the question that follows the banner is "what do I type", not
# "what changed", and the answer differs between a meercal.sh install and a
# clone. The README covers both.
UPDATE_URL = "https://github.com/ribalba/meercal#updating"

# A day between checks. Releases are not frequent enough for anything shorter
# to tell you something new, and this is a call to a third party from every
# install in existence, and being a good citizen is free here.
CHECK_INTERVAL = 24 * 3600

# After a failure, retry sooner than a day but not so soon that a machine with
# no internet spends its life retrying.
RETRY_INTERVAL = 3600

# Short: this runs in the background, but a hung connection would otherwise pin
# the "refresh in flight" flag and block the next attempt for as long as the OS
# takes to give up.
TIMEOUT = 8.0

# A version number is short and boring. Anything longer is a captive-portal
# login page or a proxy error, and parsing it as a version would be a mistake.
MAX_BODY = 64


_lock = asyncio.Lock()
_refreshing = False
_state: dict[str, object] = {
    "latest": None,      # str | None: the version on main, once known
    "checked_at": 0.0,   # monotonic clock; 0 = never
    "error": None,       # str | None: last failure, for the log and /api/version
}


def enabled() -> bool:
    return bool(get_settings().update_check)


def _stale() -> bool:
    checked = float(_state["checked_at"] or 0)
    if not checked:
        return True
    age = time.monotonic() - checked
    return age > (RETRY_INTERVAL if _state["error"] else CHECK_INTERVAL)


async def _fetch() -> None:
    """One attempt, recording whatever came of it. Never raises."""
    global _refreshing
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.get(LATEST_URL, headers={"User-Agent": f"meercal/{VERSION}"})
        response.raise_for_status()
        body = response.text[:MAX_BODY].strip()
        # Whitespace and a version's own characters, nothing else. An HTML
        # error page reaching `is_outdated` would compare as unparsable and
        # answer False anyway, but refusing it here keeps the reason legible.
        if not body or any(c not in "0123456789.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_" for c in body):
            raise ValueError("not a version string")
        _state["latest"] = body
        _state["error"] = None
    except Exception as exc:  # network, DNS, TLS, proxy, rate limit, nonsense
        _state["error"] = f"{exc.__class__.__name__}: {exc}"[:200]
        log.debug("update check failed: %s", _state["error"])
    finally:
        _state["checked_at"] = time.monotonic()
        async with _lock:
            _refreshing = False


async def _maybe_refresh() -> None:
    """Start a refresh if one is due and none is already running."""
    global _refreshing
    if not enabled() or not _stale():
        return
    async with _lock:
        if _refreshing:
            return
        _refreshing = True
    # Not awaited: the request that noticed the cache was stale must not wait
    # for github. The task holds its own reference until it finishes.
    asyncio.get_running_loop().create_task(_fetch())


async def status() -> dict:
    """What /api/version answers with. Always immediate."""
    await _maybe_refresh()
    latest = _state["latest"]
    return {
        "name": "meercal",
        "version": VERSION,
        "latest": latest,
        "update_available": bool(latest) and is_outdated(VERSION, str(latest)),
        "update_url": UPDATE_URL,
        "check_enabled": enabled(),
        # Reported rather than hidden: "the check is failing" and "you are up to
        # date" look identical in the UI, and only one of them is true.
        "error": _state["error"],
    }


def reset_for_tests() -> None:
    _state.update({"latest": None, "checked_at": 0.0, "error": None})
