"""The update check: the one outbound call the server makes.

Tested for the three things that matter more than the feature: it never
blocks, it never fails loudly, and `update_check = false` means no request at
all rather than a hidden banner.
"""

from __future__ import annotations

import pytest

from core.version import VERSION, is_outdated


@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("0.1.0", "0.2.0", True),
        ("0.1.0", "0.1.1", True),
        # Numbers, not text: "0.10.0" sorts before "0.9.0" as a string and is
        # the newer release.
        ("0.9.0", "0.10.0", True),
        ("0.2.0", "0.2.0", False),
        ("0.3.0", "0.2.9", False),
        ("0.2", "0.2.0", False),        # the same release, written shorter
        ("0.1.0", "v0.1.1", True),      # a tag, should it ever be one
        ("0.1.0", "0.1.1-rc1", True),
        # A proxy's error page must never look like a release.
        ("0.1.0", "<!DOCTYPE html>", False),
        ("0.1.0", "", False),
        ("", "0.2.0", False),
    ],
)
def test_is_outdated(current, latest, expected):
    assert is_outdated(current, latest) is expected


@pytest.fixture()
def updates(monkeypatch):
    from core.config import get_settings

    import app.updates as module

    module.reset_for_tests()
    get_settings.cache_clear()
    yield module
    module.reset_for_tests()
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_status_answers_before_the_network_does(updates, monkeypatch):
    """The first call must not wait on github."""
    started = []

    async def never_finishes(*_a, **_kw):
        started.append(True)

    monkeypatch.setattr(updates, "_fetch", never_finishes)
    status = await updates.status()
    assert status["version"] == VERSION
    assert status["latest"] is None            # not known yet
    assert status["update_available"] is False  # and so: no banner
    assert status["check_enabled"] is True


@pytest.mark.anyio
async def test_a_failed_check_is_reported_not_raised(updates, monkeypatch):
    class Boom:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def get(self, *_a, **_kw): raise OSError("network is unreachable")

    monkeypatch.setattr(updates.httpx, "AsyncClient", Boom)
    await updates._fetch()
    status = await updates.status()
    assert status["latest"] is None
    assert "OSError" in status["error"]
    assert status["update_available"] is False


@pytest.mark.anyio
async def test_a_newer_version_sets_the_flag(updates, monkeypatch):
    class Response:
        text = "99.0.0\n"
        def raise_for_status(self): pass

    class Client:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def get(self, *_a, **_kw): return Response()

    monkeypatch.setattr(updates.httpx, "AsyncClient", Client)
    await updates._fetch()
    status = await updates.status()
    assert status["latest"] == "99.0.0"
    assert status["update_available"] is True
    assert status["error"] is None


@pytest.mark.anyio
async def test_an_html_error_page_is_not_a_version(updates, monkeypatch):
    class Response:
        text = "<!DOCTYPE html><html><body>captive portal</body></html>"
        def raise_for_status(self): pass

    class Client:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def get(self, *_a, **_kw): return Response()

    monkeypatch.setattr(updates.httpx, "AsyncClient", Client)
    await updates._fetch()
    status = await updates.status()
    assert status["latest"] is None
    assert status["update_available"] is False
    assert status["error"]


@pytest.mark.anyio
async def test_switching_it_off_makes_no_request_at_all(updates, monkeypatch):
    calls = []

    async def counted():
        calls.append(True)

    monkeypatch.setenv("UPDATE_CHECK", "false")
    from core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(updates, "_fetch", counted)

    status = await updates.status()
    assert status["check_enabled"] is False
    assert calls == []
