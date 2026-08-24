"""The optional password gate.

A localhost install is open: there is nothing between the browser and the app
but the loopback interface, and a password there protects nothing that the
user's own login has not already protected. Setting ``server.password`` turns
the gate on, and with it the requirement that nothing at all is served over a
plaintext connection to anywhere but loopback, because a browser that gets the
page gets the login form, and by then the password has already crossed the
network whatever the server says afterwards.

The session cookie is a signed value rather than a database row: this app has
one user and one password, so a revocation list would be a table with nothing
in it. Changing ``secret_key`` logs every browser out, which is the revocation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Cookie, Header, HTTPException, Request, status

from core.config import get_settings

settings = get_settings()

COOKIE = "meercal_session"
# A month. Long, because the alternative to a long session on a personal
# calendar is a password typed so often it ends up in a password manager's
# autofill anyway, and the cookie is HttpOnly and Secure where it matters.
MAX_AGE = 30 * 24 * 3600

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _sign(payload: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue_token(now: float | None = None) -> str:
    issued = int(now or time.time())
    payload = f"{issued}"
    return f"{payload}.{_sign(payload)}"


def token_valid(token: str, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        issued = int(payload)
    except ValueError:
        return False
    return (now or time.time()) - issued < MAX_AGE


def password_ok(given: str) -> bool:
    # Constant-time: the comparison is over a secret, and a timing difference
    # here is a free oracle for anything that can reach the port.
    return bool(settings.server_password) and hmac.compare_digest(
        given or "", settings.server_password
    )


def is_secure_request(request: Request) -> bool:
    """Would this request's password stay off the wire?"""
    host = request.client.host if request.client else ""
    if host in LOOPBACK:
        return True
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip() == "https" and host in set(settings.trusted_proxies)


def require_auth(
    request: Request,
    session: str | None = Cookie(default=None, alias=COOKIE),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: a no-op unless a password is configured."""
    if not settings.server_password:
        return
    if not is_secure_request(request):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "meercal is password-protected, so it refuses to answer over plain HTTP. "
            "Use https, or reach it on loopback.",
        )
    if session and token_valid(session):
        return
    if authorization and authorization.lower().startswith("bearer "):
        if password_ok(authorization[7:].strip()):
            return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


def new_secret() -> str:
    return secrets.token_urlsafe(48)
