"""Google, which is CalDAV with a different way of saying who you are.

Basic auth to Google's CalDAV endpoint has been off for years, so this is
OAuth2 or nothing — an App Password does not open it the way it opens IMAP for
meerail. What that buys, though, is that once there is a bearer token the
protocol is the same CalDAV as everything else, so this file is only the token.

Minting the refresh token is a one-time job: create an OAuth *Desktop* client in
the Google Cloud Console, enable the CalDAV API, then

    python -m agent.google_auth --client-id … --client-secret …

which prints the three lines to paste into meercal.toml.
"""

from __future__ import annotations

import time

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"
# Where CalDAV discovery starts for a Google account. {user} is the address.
PRINCIPAL = "https://apidata.googleusercontent.com/caldav/v2/{user}/user"

# access tokens last an hour; refresh a minute early rather than on the 401.
_LEEWAY = 60
_cache: dict[str, tuple[str, float]] = {}


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    key = refresh_token[-16:]
    cached = _cache.get(key)
    if cached and cached[1] - _LEEWAY > time.time():
        return cached[0]
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Google refused the refresh token: {response.text[:200]}")
    payload = response.json()
    token = payload["access_token"]
    _cache[key] = (token, time.time() + int(payload.get("expires_in", 3600)))
    return token
