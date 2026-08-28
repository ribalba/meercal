"""The plaintext refusal, and what lifts it.

``server.password`` turns on a gate whose first rule is that nothing is served
over a plaintext connection to anywhere but loopback: the browser that gets the
page gets the login form, and the password has crossed the network by then
whatever the server says afterwards. Behind a proxy that terminates TLS the
connection the server sees *is* plaintext, so the refusal has to be lifted by
``server.trusted_proxies`` -- and the shape of that setting is what most of
this file is about, because getting it wrong is a 403 on every request with no
way to tell from the outside which half is at fault.
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")

from starlette.requests import Request

from app import security


@pytest.fixture
def gated(monkeypatch):
    """A configured password, and no trusted proxies until a test says so."""
    monkeypatch.setattr(security.settings, "server_password", "hunter2")
    monkeypatch.setattr(security.settings, "trusted_proxies", [])
    return security.settings


def request_from(host: str, scheme: str = "http", **headers) -> Request:
    raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": scheme,
            "headers": raw,
            "client": (host, 44444),
            "server": ("meercal", 8000),
        }
    )


def test_loopback_needs_no_tls(gated):
    # The localhost install: nothing between the browser and the app but the
    # loopback interface, which is the case the password is optional for.
    assert security.is_secure_request(request_from("127.0.0.1"))
    assert security.is_secure_request(request_from("::1"))


def test_plain_http_from_anywhere_else_is_refused(gated):
    assert not security.is_secure_request(request_from("203.0.113.9"))


def test_a_forwarded_header_alone_proves_nothing(gated):
    # Anything that can reach the port can send this. Without a trusted proxy
    # it is a claim from a stranger, not a fact about the connection.
    assert not security.is_secure_request(
        request_from("203.0.113.9", x_forwarded_proto="https")
    )


@pytest.mark.parametrize(
    "trusted",
    [
        ["10.0.1.7"],           # the literal address
        ["10.0.0.0/8"],         # the range it is in, which is what survives a redeploy
        ["*"],                  # the port is the boundary
        ["proxy.internal", "10.0.0.0/8"],  # a name it cannot parse, and a range it can
    ],
)
def test_a_trusted_proxy_is_believed(gated, monkeypatch, trusted):
    monkeypatch.setattr(security.settings, "trusted_proxies", trusted)
    assert security.is_secure_request(
        request_from("10.0.1.7", x_forwarded_proto="https")
    )


def test_a_chain_of_proxies_is_read_from_the_left(gated, monkeypatch):
    # Two proxies deep the header is a list, and uvicorn's middleware leaves
    # the scheme alone rather than guess at it. The browser's own hop is the
    # first entry, and it is the one that says whether the password was
    # encrypted when it was typed.
    monkeypatch.setattr(security.settings, "trusted_proxies", ["10.0.0.0/8"])
    assert security.is_secure_request(
        request_from("10.0.1.7", x_forwarded_proto="https,https")
    )
    assert not security.is_secure_request(
        request_from("10.0.1.7", x_forwarded_proto="http,https")
    )


def test_trust_does_not_leak_across_ranges(gated, monkeypatch):
    monkeypatch.setattr(security.settings, "trusted_proxies", ["10.0.0.0/8"])
    assert not security.is_secure_request(
        request_from("172.17.0.4", x_forwarded_proto="https")
    )
    # An IPv4 address is not in an IPv6 network, and asking must not raise.
    monkeypatch.setattr(security.settings, "trusted_proxies", ["fd00::/8"])
    assert not security.is_secure_request(
        request_from("10.0.1.7", x_forwarded_proto="https")
    )


def test_https_needs_no_proxy_at_all(gated):
    # TLS ended here, or the middleware in app/main.py already rewrote the
    # scheme from a header it was configured to believe.
    assert security.is_secure_request(request_from("203.0.113.9", scheme="https"))
