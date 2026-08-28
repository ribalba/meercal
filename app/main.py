"""meercal-server: the web layer.

It reads the database and enqueues what the user does. It never speaks CalDAV,
holds no calendar credentials, and has no code path that could send one
anywhere. That split is meerail's, and it is here for the same reason: the
process that talks to your family's iCloud account should be the one running on
your own machine, not the one in a container behind a web server.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from core.config import get_settings
from core.database import init_db
from core.version import VERSION
from .limits import BodySizeLimitMiddleware
from .routers import (
    auth, calendars, contacts, events, imports, reminders, search, state, sync, version,
)
from .security import PLAINTEXT_REFUSAL, is_secure_request, require_secure

settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"


async def lifespan(_app: FastAPI):
    # Waits for Postgres and creates what is missing. Compose starts the two
    # together and `depends_on` only waits for the container.
    init_db()
    yield


app = FastAPI(
    title="meercal",
    version=VERSION,
    description="The meercal calendar: many calendars, seen at once.",
    lifespan=lifespan,
)

if settings.trusted_proxies:
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)

# Ahead of the router, because the body of an upload is parsed to resolve
# `UploadFile` before any dependency runs -- the password gate included. The
# headroom over the import cap is the multipart framing and the form fields
# that travel beside the file.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=imports.MAX_BYTES + 1024 * 1024)

for module in (auth, version, state, calendars, events, search, contacts, sync, reminders, imports):
    app.include_router(module.router)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness for the container. Deliberately outside the password gate and
    deliberately not touching the database: it answers "is this process up",
    and a health check that fails because Postgres is restarting takes the web
    layer down with it for no reason."""
    return {"ok": True, "version": VERSION}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index(_: None = Depends(require_secure)) -> FileResponse:
    # The connection is checked, the session is not: the shell holds no
    # calendar and its whole job on a password-protected install is to put the
    # login form on screen. See app/security.py.
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(404)
async def not_found(request: Request, _exc) -> JSONResponse | FileResponse:
    # Anything under /api that does not exist is an API error; anything else is
    # a deep link into the single-page app and gets the app.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # A deep link is the same shell by another name, and it is refused over
    # plaintext for the same reason. An exception handler cannot raise its way
    # out, so the refusal is written rather than thrown.
    if settings.server_password and not is_secure_request(request):
        return JSONResponse({"detail": PLAINTEXT_REFUSAL}, status_code=403)
    return FileResponse(STATIC_DIR / "index.html")
