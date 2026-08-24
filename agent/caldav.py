"""A small CalDAV client: PROPFIND, REPORT, PUT, DELETE and nothing else.

Hand-rolled rather than pulled in, for the same reason meerail speaks IMAP
itself: what this program needs is four requests, and owning them means the
failures are ours to read. The XML below is the whole protocol as meercal uses
it.

Two things about real servers shape this file:

* **iCloud moves you.** Discovery on ``caldav.icloud.com`` answers with a
  principal on a personal host (``p42-caldav.icloud.com``), and every later
  request has to go there. Hrefs come back as paths, so they are resolved
  against whatever host actually answered.
* **Sync tokens are optional.** RFC 6578 gives an incremental listing, and a
  pass over a quiet calendar is then one request. Servers that do not implement
  it, or that expire a token, get a full listing of hrefs and etags instead,
  which is still cheap because the bodies are only fetched for what changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"
APPLE = "http://apple.com/ns/ical/"

NS = {"d": DAV, "c": CALDAV, "cs": CS, "a": APPLE}

_PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

_PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>"""

_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">
  <d:prop>
    <d:resourcetype/>
    <d:displayname/>
    <d:current-user-privilege-set/>
    <cs:getctag/>
    <d:sync-token/>
    <a:calendar-color/>
    <c:supported-calendar-component-set/>
    <c:calendar-timezone/>
  </d:prop>
</d:propfind>"""

_PROPFIND_ETAGS = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>"""


class CalDAVError(RuntimeError):
    pass


@dataclass
class RemoteCalendar:
    url: str
    name: str
    color: str = ""
    ctag: str = ""
    sync_token: str = ""
    read_only: bool = False
    tz_id: str = ""


@dataclass
class Change:
    """One resource the server says is different from what we hold."""

    href: str
    etag: str = ""
    deleted: bool = False
    ics: str = ""


@dataclass
class Listing:
    changes: list[Change] = field(default_factory=list)
    sync_token: str = ""
    # True when the listing is the *whole* collection, which is the only case
    # in which anything missing from it may be deleted locally.
    complete: bool = False


class CalDAVClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        bearer: str = "",
        timeout: float = 30.0,
    ):
        headers = {"User-Agent": "meercal/agent", "Content-Type": "application/xml; charset=utf-8"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        self.base_url = base_url
        self._client = httpx.Client(
            headers=headers,
            auth=(username, password) if password and not bearer else None,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CalDAVClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- plumbing ---------------------------------------------------------

    def _request(self, method: str, url: str, body: str = "", depth: str = "0", **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if depth is not None:
            headers["Depth"] = depth
        response = self._client.request(method, url, content=body.encode() if body else None,
                                        headers=headers, **kw)
        if response.status_code >= 400:
            raise CalDAVError(f"{method} {url} -> {response.status_code} {response.text[:200]}")
        return response

    @staticmethod
    def _resolve(base: httpx.URL | str, href: str) -> str:
        """An href against the URL that actually answered.

        Servers return paths, and after a redirect the path belongs to the host
        we were sent to, not the one in the configuration file. Getting this
        wrong is how an iCloud account syncs once and then 404s forever.
        """
        return urljoin(str(base), href)

    def _multistatus(self, response: httpx.Response) -> list[ET.Element]:
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise CalDAVError(f"unreadable multistatus from {response.url}: {exc}") from exc
        return root.findall("d:response", NS)

    # --- discovery --------------------------------------------------------

    def principal(self) -> str:
        response = self._request("PROPFIND", self.base_url, _PROPFIND_PRINCIPAL)
        for entry in self._multistatus(response):
            href = entry.find(".//d:current-user-principal/d:href", NS)
            if href is not None and href.text:
                return self._resolve(response.url, href.text)
        # Some servers answer discovery on the account URL itself.
        return str(response.url)

    def calendar_home(self, principal_url: str) -> str:
        response = self._request("PROPFIND", principal_url, _PROPFIND_HOME)
        for entry in self._multistatus(response):
            href = entry.find(".//c:calendar-home-set/d:href", NS)
            if href is not None and href.text:
                return self._resolve(response.url, href.text)
        raise CalDAVError(f"no calendar-home-set at {principal_url}")

    def calendars(self, home_url: str) -> list[RemoteCalendar]:
        response = self._request("PROPFIND", home_url, _PROPFIND_CALENDARS, depth="1")
        out: list[RemoteCalendar] = []
        for entry in self._multistatus(response):
            if entry.find(".//d:resourcetype/c:calendar", NS) is None:
                continue  # the home collection itself, address books, inboxes
            components = [
                el.get("name")
                for el in entry.findall(".//c:supported-calendar-component-set/c:comp", NS)
            ]
            if components and "VEVENT" not in components:
                continue  # a task list or a journal; meercal draws events

            href = entry.find("d:href", NS)
            if href is None or not href.text:
                continue
            name = entry.find(".//d:displayname", NS)
            color = entry.find(".//a:calendar-color", NS)
            ctag = entry.find(".//cs:getctag", NS)
            token = entry.find(".//d:sync-token", NS)
            tz = entry.find(".//c:calendar-timezone", NS)
            privileges = [
                el.tag.split("}")[-1]
                for el in entry.findall(".//d:current-user-privilege-set/d:privilege/*", NS)
            ]
            out.append(
                RemoteCalendar(
                    url=self._resolve(response.url, href.text),
                    name=(name.text or "").strip() if name is not None else "",
                    # Apple writes #RRGGBBAA; the alpha is not ours to keep.
                    color=(color.text or "")[:7] if color is not None and color.text else "",
                    ctag=(ctag.text or "") if ctag is not None else "",
                    sync_token=(token.text or "") if token is not None else "",
                    read_only=bool(privileges) and "write-content" not in privileges,
                    tz_id=_tz_from_vtimezone(tz.text if tz is not None else ""),
                )
            )
        return out

    # --- listing ----------------------------------------------------------

    def changes(self, calendar_url: str, sync_token: str = "") -> Listing:
        """What is different since ``sync_token``, or everything if it cannot say."""
        if sync_token:
            try:
                return self._sync_collection(calendar_url, sync_token)
            except CalDAVError:
                # An expired or unknown token is answered with 403/409 by most
                # servers. That is not a failure, it is a request for a full
                # listing, which is exactly what the fallback does.
                pass
        listing = self._etag_listing(calendar_url)
        try:
            fresh = self._sync_collection(calendar_url, "")
            listing.sync_token = fresh.sync_token
        except CalDAVError:
            listing.sync_token = ""
        return listing

    def _sync_collection(self, calendar_url: str, sync_token: str) -> Listing:
        body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<d:sync-collection xmlns:d="DAV:">'
            f"<d:sync-token>{sync_token}</d:sync-token>"
            "<d:sync-level>1</d:sync-level>"
            "<d:prop><d:getetag/></d:prop>"
            "</d:sync-collection>"
        )
        response = self._request("REPORT", calendar_url, body, depth="1")
        root = ET.fromstring(response.text)
        listing = Listing(complete=not sync_token)
        for entry in root.findall("d:response", NS):
            href = entry.find("d:href", NS)
            if href is None or not href.text:
                continue
            status_el = entry.find("d:status", NS)
            gone = status_el is not None and " 404 " in (status_el.text or "")
            etag = entry.find(".//d:getetag", NS)
            listing.changes.append(
                Change(
                    href=self._resolve(response.url, href.text),
                    etag=(etag.text or "").strip('"') if etag is not None and etag.text else "",
                    deleted=gone,
                )
            )
        token = root.find("d:sync-token", NS)
        listing.sync_token = (token.text or "") if token is not None else ""
        return listing

    def _etag_listing(self, calendar_url: str) -> Listing:
        response = self._request("PROPFIND", calendar_url, _PROPFIND_ETAGS, depth="1")
        listing = Listing(complete=True)
        for entry in self._multistatus(response):
            href = entry.find("d:href", NS)
            etag = entry.find(".//d:getetag", NS)
            if href is None or not href.text or etag is None:
                continue  # the collection itself has no etag
            url = self._resolve(response.url, href.text)
            if url.rstrip("/") == calendar_url.rstrip("/"):
                continue
            listing.changes.append(
                Change(href=url, etag=(etag.text or "").strip('"'))
            )
        return listing

    # --- bodies -----------------------------------------------------------

    def fetch(self, calendar_url: str, hrefs: list[str], batch: int = 50) -> list[Change]:
        """The iCalendar text for a set of resources, in batches.

        calendar-multiget in one request per batch rather than a GET each: a
        first sync of a family calendar is a few thousand resources, and a
        round trip apiece is the difference between a minute and an hour.
        """
        out: list[Change] = []
        for i in range(0, len(hrefs), batch):
            chunk = hrefs[i : i + batch]
            body = (
                '<?xml version="1.0" encoding="utf-8" ?>'
                '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
                + "".join(f"<d:href>{_xml_escape(h)}</d:href>" for h in chunk)
                + "</c:calendar-multiget>"
            )
            response = self._request("REPORT", calendar_url, body, depth="1")
            for entry in self._multistatus(response):
                href = entry.find("d:href", NS)
                data = entry.find(".//c:calendar-data", NS)
                etag = entry.find(".//d:getetag", NS)
                if href is None or data is None or not (data.text or "").strip():
                    continue
                out.append(
                    Change(
                        href=self._resolve(response.url, href.text or ""),
                        etag=(etag.text or "").strip('"') if etag is not None and etag.text else "",
                        ics=data.text,
                    )
                )
        return out

    # --- writing ----------------------------------------------------------

    def put(self, url: str, ics: str, etag: str = "") -> str:
        """Write one resource. Returns the new etag when the server gives one.

        ``If-Match`` on an update and ``If-None-Match: *`` on a create are what
        make this safe against the other end having changed underneath us: the
        server refuses with 412 rather than overwriting, and the sync pass that
        follows brings the newer version down.
        """
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers["If-Match"] = f'"{etag}"' if etag else "*"
        if not etag:
            headers = {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}
        response = self._request("PUT", url, ics, depth=None, headers=headers)
        return (response.headers.get("ETag", "") or "").strip('"')

    def delete(self, url: str, etag: str = "") -> None:
        headers = {"If-Match": f'"{etag}"'} if etag else {}
        self._request("DELETE", url, depth=None, headers=headers)


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tz_from_vtimezone(text: str) -> str:
    """The TZID out of a calendar's default VTIMEZONE, if it has one."""
    match = re.search(r"^TZID:(.+)$", text or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def resource_url(calendar_url: str, uid: str) -> str:
    """Where a new event goes. The UID as the filename is the convention every
    server follows, and it makes the resource findable again from the event."""
    safe = re.sub(r"[^A-Za-z0-9._@-]", "-", uid)
    return urljoin(calendar_url if calendar_url.endswith("/") else calendar_url + "/", f"{safe}.ics")
