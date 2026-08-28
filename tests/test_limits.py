"""The body cap that sits in front of the router.

What makes this worth a test of its own is the ordering, not the arithmetic.
FastAPI parses a multipart body to fill in an ``UploadFile`` before it runs the
route's dependencies, so ``require_auth`` is not what stops an oversized upload
and ``imports.MAX_BYTES`` is checked too late to stop the reading of one. The
assertion that matters in every case below is therefore ``reached``: the route
must not have run.
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.limits import BodySizeLimitMiddleware

CAP = 1024 * 1024  # A megabyte, so the tests stay small.


@pytest.fixture
def app():
    """A route that reads its body, wrapped in the cap. ``reached`` records
    whether the request got that far."""
    state = {"reached": False}

    async def sink(request):
        body = await request.body()
        state["reached"] = True
        return PlainTextResponse(str(len(body)))

    inner = Starlette(routes=[Route("/sink", sink, methods=["POST"])])
    wrapped = BodySizeLimitMiddleware(inner, max_bytes=CAP)
    return wrapped, state


def test_under_the_cap_passes_through(app):
    wrapped, state = app
    with TestClient(wrapped) as c:
        r = c.post("/sink", content=b"x" * (CAP // 2))
    assert r.status_code == 200
    assert r.text == str(CAP // 2)
    assert state["reached"]


def test_declared_oversize_is_refused_without_reading(app):
    """Content-Length over the cap: refused on the header alone."""
    wrapped, state = app
    with TestClient(wrapped) as c:
        r = c.post("/sink", content=b"x" * (CAP * 4))
    assert r.status_code == 413
    assert "larger than" in r.json()["detail"]
    assert not state["reached"]


def test_chunked_oversize_is_refused_while_reading(app):
    """No Content-Length to check, so the cap has to count as it goes."""
    wrapped, state = app

    def chunks():
        for _ in range(8):
            yield b"x" * (CAP // 2)

    with TestClient(wrapped) as c:
        r = c.post("/sink", content=chunks())
    assert r.status_code == 413
    assert "larger than" in r.json()["detail"]
    assert not state["reached"]


def test_exactly_at_the_cap_is_allowed(app):
    wrapped, state = app
    with TestClient(wrapped) as c:
        r = c.post("/sink", content=b"x" * CAP)
    assert r.status_code == 200
    assert state["reached"]


def test_unparseable_content_length_is_not_ours_to_judge(app):
    """A malformed header is the server's error to report, not a 413."""
    wrapped, _ = app
    with TestClient(wrapped) as c:
        r = c.post("/sink", content=b"hello", headers={"content-length": "banana"})
    assert r.status_code != 413


def test_a_get_is_untouched(app):
    wrapped, _ = app
    with TestClient(wrapped) as c:
        r = c.get("/sink")
    assert r.status_code == 405  # the route is POST-only; it was still routed
