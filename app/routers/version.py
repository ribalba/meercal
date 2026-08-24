from fastapi import APIRouter

from core.config import get_settings
from core.version import VERSION

router = APIRouter(prefix="/api", tags=["version"])
settings = get_settings()


@router.get("/version")
def version() -> dict:
    return {"version": VERSION, "name": "meercal"}
