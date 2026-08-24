"""What version this server is, and whether a newer one is out.

Behind the UI auth gate like everything else under /api: the version of the
software you are running is exactly the sort of thing an internet-wide scanner
would like to collect, and there is no reason for it to be readable by anyone
who cannot already read the calendar. /healthz stays open for the container's
health check; this does not.
"""

from fastapi import APIRouter, Depends

from .. import updates
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["version"], dependencies=[Depends(require_auth)])


@router.get("/version")
async def version_info() -> dict:
    # Answers from cache and refreshes in the background; see app/updates.py.
    return await updates.status()
