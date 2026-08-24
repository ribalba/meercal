"""Capture the website screenshots from a running meercal.

Drives the real UI in Chromium rather than mocking anything, so a shot that
renders here is a shot the app actually produces.

    make -C .. seed                    # the demo calendars
    python website/screenshots/shoot.py
    python website/screenshots/shoot.py --only ribbon

Output lands in website/public/img/screenshots/ at 2x device scale — the page
serves 2880x1800 files and displays them at half that, so they stay sharp on
retina panels.

The one thing that bites: the view the app opens in is remembered server-side
(`/api/prefs`), so every shot presses its own view key rather than trusting
whatever the last run left behind.
"""

from __future__ import annotations

import argparse
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("MEERCAL_URL", "http://127.0.0.1:8010")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public", "img", "screenshots")

# 1440x900 at 2x.
VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2

# Long enough for the range request behind a view switch to land and paint.
SETTLE = 1400


def boot(page, view: str) -> None:
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(600)
    page.keyboard.press({"ribbon": "r", "week": "w", "month": "m", "day": "d"}[view])
    page.wait_for_timeout(SETTLE)


def shot(page, name: str) -> None:
    path = os.path.normpath(os.path.join(OUT, f"{name}{SUFFIX}.png"))
    page.screenshot(path=path)
    print(f"  {os.path.relpath(path)}")


# Appended to every filename this pass writes: "" for light, "-dark" for the
# dark one. Set once per context rather than renamed afterwards — renaming
# after the fact overwrites the light shot taken moments earlier.
SUFFIX = ""

SHOTS = {}


def register(fn):
    SHOTS[fn.__name__] = fn
    return fn


@register
def ribbon(page):
    boot(page, "ribbon")
    shot(page, "ribbon")


@register
def week(page):
    boot(page, "week")
    shot(page, "week")


@register
def month(page):
    boot(page, "month")
    shot(page, "month")


@register
def spans(page):
    """The Ribbon with everything but the long events filtered away."""
    boot(page, "ribbon")
    page.fill("#filter-input", "is:span")
    page.wait_for_timeout(SETTLE)
    shot(page, "spans")


@register
def search(page):
    """Enter in the filter bar searches every calendar, hidden ones included."""
    boot(page, "ribbon")
    page.fill("#filter-input", "standup")
    page.keyboard.press("Enter")
    page.wait_for_timeout(SETTLE)
    shot(page, "search")


@register
def event(page):
    """The event panel, opened on something with people on it."""
    boot(page, "week")
    page.click(".wk-event:has-text('Jour fixe')")
    page.wait_for_timeout(SETTLE)
    shot(page, "event")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", choices=sorted(SHOTS), help="one shot, repeatable")
    parser.add_argument("--dark", action="store_true", help="also capture the dark variants")
    args = parser.parse_args()
    names = args.only or sorted(SHOTS)

    os.makedirs(os.path.normpath(OUT), exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark") if args.dark else ("light",):
            global SUFFIX
            SUFFIX = "-dark" if scheme == "dark" else ""
            context = browser.new_context(
                viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme=scheme
            )
            page = context.new_page()
            failures = []
            page.on("pageerror", lambda e: failures.append(str(e)))
            for name in names:
                print(f"{scheme}: {name}")
                SHOTS[name](page)
            context.close()
            if failures:
                print("page errors:", *failures, sep="\n  ", file=sys.stderr)
                return 1
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
