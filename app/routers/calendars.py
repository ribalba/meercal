"""Which calendars are drawn, what colour they are, and the sets that switch
them in one keystroke.

Visibility is server state, not browser state. With twenty calendars, "which
ones am I looking at" is a real piece of the working set — it should survive a
reload, a second window, and the laptop being closed, and localStorage survives
none of those reliably.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import Calendar, CalendarSet, CalendarSetMember, Setting
from ..serialize import calendar_json
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["calendars"], dependencies=[Depends(require_auth)])


class CalendarPatch(BaseModel):
    visible: bool | None = None
    color: str | None = None
    display_name: str | None = None
    position: int | None = None


@router.patch("/calendars/{cal_id}")
def patch_calendar(cal_id: int, body: CalendarPatch, db: Session = Depends(get_db)) -> dict:
    cal = db.get(Calendar, cal_id)
    if cal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such calendar")
    if body.visible is not None:
        cal.visible = body.visible
    if body.color:
        cal.color = body.color
    if body.display_name is not None:
        cal.display_name = body.display_name.strip()
    if body.position is not None:
        cal.position = body.position
    db.commit()
    return calendar_json(cal)


class Visibility(BaseModel):
    visible: list[int]


@router.post("/calendars/visibility")
def set_visibility(body: Visibility, db: Session = Depends(get_db)) -> dict:
    """Replace the whole visible set in one request.

    One call rather than one per calendar: applying a set of twenty calendars
    otherwise means twenty PATCHes, twenty repaints and a visibly stuttering
    sidebar. This is also what "solo this calendar" is built on.
    """
    wanted = set(body.visible)
    for cal in db.execute(select(Calendar)).scalars().all():
        cal.visible = cal.id in wanted
    db.commit()
    return {"visible": sorted(wanted)}


class SetBody(BaseModel):
    name: str
    hotkey: int | None = None
    calendars: list[int] = []


@router.post("/sets")
def create_set(body: SetBody, db: Session = Depends(get_db)) -> dict:
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A set needs a name")
    existing = db.execute(select(CalendarSet).where(CalendarSet.name == body.name)).scalar_one_or_none()
    cset = existing or CalendarSet(name=body.name.strip())
    if body.hotkey is not None:
        for other in db.execute(
            select(CalendarSet).where(CalendarSet.hotkey == body.hotkey)
        ).scalars().all():
            if other is not cset:
                other.hotkey = None
    cset.hotkey = body.hotkey
    if existing is None:
        db.add(cset)
    db.flush()
    db.execute(delete(CalendarSetMember).where(CalendarSetMember.set_id == cset.id))
    db.add_all([CalendarSetMember(set_id=cset.id, calendar_id=c) for c in body.calendars])
    db.commit()
    return {"id": cset.id, "name": cset.name, "hotkey": cset.hotkey, "calendars": body.calendars}


class SetPatch(BaseModel):
    name: str | None = None
    hotkey: int | None = None
    calendars: list[int] | None = None
    clear_hotkey: bool = False


@router.patch("/sets/{set_id}")
def update_set(set_id: int, body: SetPatch, db: Session = Depends(get_db)) -> dict:
    """Rename a set, move its key, or change what is in it.

    By id rather than by name, because renaming is one of the things this is
    for — and `hotkey` cannot express "take the key away" with a value, since
    null is also what "unchanged" looks like on a PATCH. Hence `clear_hotkey`.
    """
    cset = db.get(CalendarSet, set_id)
    if cset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such set")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A set needs a name")
        clash = db.execute(
            select(CalendarSet).where(CalendarSet.name == name, CalendarSet.id != set_id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"There is already a set called {name}")
        cset.name = name
    if body.clear_hotkey:
        cset.hotkey = None
    elif body.hotkey is not None:
        # One key, one set: taking a key means taking it off whoever had it,
        # rather than leaving two sets that both claim to answer to 3.
        for other in db.execute(
            select(CalendarSet).where(CalendarSet.hotkey == body.hotkey, CalendarSet.id != set_id)
        ).scalars().all():
            other.hotkey = None
        cset.hotkey = body.hotkey
    if body.calendars is not None:
        db.execute(delete(CalendarSetMember).where(CalendarSetMember.set_id == cset.id))
        db.add_all([CalendarSetMember(set_id=cset.id, calendar_id=c) for c in body.calendars])
    db.commit()

    members = [
        row.calendar_id
        for row in db.execute(
            select(CalendarSetMember).where(CalendarSetMember.set_id == cset.id)
        ).scalars().all()
    ]
    return {"id": cset.id, "name": cset.name, "hotkey": cset.hotkey, "calendars": members}


@router.delete("/sets/{set_id}")
def delete_set(set_id: int, db: Session = Depends(get_db)) -> dict:
    cset = db.get(CalendarSet, set_id)
    if cset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such set")
    db.delete(cset)
    db.commit()
    return {"ok": True}


@router.post("/sets/{set_id}/apply")
def apply_set(set_id: int, db: Session = Depends(get_db)) -> dict:
    cset = db.get(CalendarSet, set_id)
    if cset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such set")
    members = {
        row.calendar_id
        for row in db.execute(
            select(CalendarSetMember).where(CalendarSetMember.set_id == set_id)
        ).scalars().all()
    }
    for cal in db.execute(select(Calendar)).scalars().all():
        cal.visible = cal.id in members
    db.commit()
    return {"visible": sorted(members)}


class Prefs(BaseModel):
    value: dict


@router.put("/prefs")
def put_prefs(body: Prefs, db: Session = Depends(get_db)) -> dict:
    """The client's own state — chosen view, density, what the ribbon collapses.

    Stored whole rather than key by key: it is small, it is written on a debounce
    from one place, and a merge would only be a way for two windows to lose each
    other's changes more subtly.
    """
    row = db.get(Setting, "ui")
    if row is None:
        row = Setting(key="ui", value=body.value)
        db.add(row)
    else:
        row.value = body.value
    db.commit()
    return {"ok": True}
