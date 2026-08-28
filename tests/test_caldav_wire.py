"""What this client puts on the wire, without a server to put it to.

``test_caldav.py`` covers the conversation against a real server, and is
skipped without one. This file covers the two things a compliant server would
have let pass and iCloud does not: a multiget body naming its resources by
absolute URL, which Apple answers 404 to every href in and which arrives as a
calendar that syncs cleanly and stays empty; and a multiget naming the
collection itself, which Apple does not answer at all.
"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")

from agent.caldav import CalDAVClient  # noqa: E402

CAL = "https://p106-caldav.icloud.com/1234/calendars/family/"

MULTISTATUS = """<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <response>
    <href>/1234/calendars/family/a.ics</href>
    <propstat>
      <prop>
        <getetag>"abc"</getetag>
        <C:calendar-data>BEGIN:VCALENDAR
END:VCALENDAR
</C:calendar-data>
      </prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
</multistatus>
"""


@pytest.fixture()
def client():
    """A client whose transport answers from memory and records the bodies."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(207, text=MULTISTATUS)

    c = CalDAVClient(CAL)
    c.close()
    c._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=CAL)
    c.sent = sent
    yield c
    c.close()


def test_multiget_names_resources_by_path(client):
    client.fetch(CAL, [CAL + "a.ics", CAL + "b.ics"])
    body = client.sent[0]
    assert "<d:href>/1234/calendars/family/a.ics</d:href>" in body
    assert "<d:href>/1234/calendars/family/b.ics</d:href>" in body
    assert "https://" not in body.split("<d:href>", 1)[1]


def test_multiget_answers_with_absolute_urls(client):
    # The path goes out, an absolute URL comes back: everything downstream
    # keys events by the same URL the listing gave, host included.
    got = client.fetch(CAL, [CAL + "a.ics"])
    assert [c.href for c in got] == [CAL + "a.ics"]
    assert got[0].etag == "abc"


SYNC_REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:">
  <response>
    <href>/1234/calendars/family/</href>
    <propstat><prop><getetag>"kkx47crs"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat>
  </response>
  <response>
    <href>/1234/calendars/family/a.ics</href>
    <propstat><prop><getetag>"abc"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat>
  </response>
  <sync-token>http://icloud.com/ns/sync/2</sync-token>
</multistatus>
"""

ETAG_LISTING = SYNC_REPORT.replace("<sync-token>http://icloud.com/ns/sync/2</sync-token>", "")


def _listing(body: str):
    """A client whose one request is answered with ``body``."""
    c = CalDAVClient(CAL)
    c.close()
    c._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(207, text=body)), base_url=CAL
    )
    return c


@pytest.mark.parametrize(
    "body, listing",
    [
        (SYNC_REPORT, lambda c: c._sync_collection(CAL, "http://icloud.com/ns/sync/1")),
        (ETAG_LISTING, lambda c: c._etag_listing(CAL)),
    ],
)
def test_listing_drops_the_collection_itself(body, listing):
    # iCloud reports the calendar alongside its resources whenever the
    # calendar's own properties changed. Carried into a multiget it hangs the
    # request until the read timeout, and the calendar never syncs again.
    with _listing(body) as c:
        assert [ch.href for ch in listing(c).changes] == [CAL + "a.ics"]


def test_multiget_drops_the_collection_itself(client):
    # Belt to the listing's braces: whatever puts it there, it never goes out.
    client.fetch(CAL, [CAL, CAL + "a.ics"])
    body = client.sent[0]
    assert body.count("<d:href>") == 1
    assert "<d:href>/1234/calendars/family/a.ics</d:href>" in body


def test_multiget_of_nothing_asks_nothing(client):
    assert client.fetch(CAL, [CAL]) == []
    assert client.sent == []


def test_collection_href_matches_across_ports():
    # The URL we hold has no port; iCloud's redirect answers on :443.
    assert CalDAVClient._is_collection("https://p106-caldav.icloud.com:443/1234/calendars/family/", CAL)
    assert not CalDAVClient._is_collection(CAL + "a.ics", CAL)


# --- a paged sync-collection ------------------------------------------------
#
# Google answers a sync report ~250 resources at a time and hands back a token
# to carry on from. Asking once a pass is not a slow catch-up but no catch-up
# at all: the caller records the collection as seen, the ctag then
# short-circuits every following pass, and a change on page two is never
# fetched. That is an acceptance that never turns up on screen.

def _page(names: list[str], token: str) -> str:
    rows = "".join(
        f"<response><href>/1234/calendars/family/{n}</href>"
        f"<propstat><prop><getetag>\"{n}\"</getetag></prop>"
        f"<status>HTTP/1.1 200 OK</status></propstat></response>"
        for n in names
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><multistatus xmlns="DAV:">'
            f"{rows}<sync-token>{token}</sync-token></multistatus>")


def _paged(pages: list[str]):
    """A client that answers each REPORT with the next page, then repeats the
    last one -- which is how a real server says there is nothing more."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(207, text=pages[min(len(seen) - 1, len(pages) - 1)])

    c = CalDAVClient(CAL)
    c.close()
    c._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=CAL)
    c.seen = seen
    return c


def test_every_page_of_a_sync_report_is_walked():
    client = _paged([
        _page(["a.ics", "b.ics"], "tok-2"),
        _page(["c.ics"], "tok-3"),
        _page([], "tok-3"),          # same token back: nothing further
    ])
    listing = client.changes(CAL, "tok-1")
    assert [c.href.rsplit("/", 1)[-1] for c in listing.changes] == ["a.ics", "b.ics", "c.ics"]
    assert listing.sync_token == "tok-3"
    assert listing.drained is True
    client.close()


def test_a_backlog_longer_than_one_pass_is_not_recorded_as_caught_up():
    """Each page carries a new token and more work, forever. The walk stops --
    it is a bound, not a promise -- but says it did not finish, and sync.py
    leaves the ctag alone so the next pass picks up where this one gave up."""
    pages = [_page([f"{i}.ics"], f"tok-{i}") for i in range(CalDAVClient.MAX_SYNC_PAGES + 5)]
    client = _paged(pages)
    listing = client.changes(CAL, "tok-start")
    assert listing.drained is False
    assert len(client.seen) == CalDAVClient.MAX_SYNC_PAGES
    client.close()


def test_a_token_the_server_will_not_advance_ends_the_walk():
    """Rather than asking the same question until the timeout."""
    client = _paged([_page(["a.ics"], "tok-1")])   # the token it was given back
    listing = client.changes(CAL, "tok-1")
    assert listing.drained is True
    assert len(client.seen) == 1
    client.close()
