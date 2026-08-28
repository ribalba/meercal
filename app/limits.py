"""A ceiling on how much body the server will read.

The password gate does not help here. FastAPI parses a multipart body to
resolve ``UploadFile`` *before* it runs a route's dependencies, so by the time
``require_auth`` says no, the parser has already done the work and spooled the
upload to disk. Anyone who can reach the port can therefore spend the server's
memory and disk without ever holding a password.

``imports.MAX_BYTES`` does not help either, for the same reason one step later:
it is checked in ``_read``, which is route code, and route code runs after the
parse. It is a limit on what will be *imported*, not on what will be *read*.

So the limit lives here instead, in front of the router, where it is the first
thing a request meets. The usual place for this is a reverse proxy
(``client_max_body_size`` in nginx), and if there is one in front of this
process it should be set there as well -- but meercal is installed as a
container that publishes a port, commonly with nothing in front of it at all,
and a limit that only exists in a proxy nobody deployed is not a limit.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """Refuse bodies past ``max_bytes``, as early as the request allows."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The cheap case, and the one nearly every real client hits: the size
        # is declared up front, so nothing has to be read to refuse it.
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._refuse(send)
                    return
            except ValueError:
                pass  # Not a number. Starlette will reject it; not our business.

        # The other case: a chunked body, which announces nothing and has to be
        # counted as it arrives.
        received = 0
        overflowed = False
        started = False

        async def counted_receive() -> Message:
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # A disconnect rather than an exception: the body parser is
                    # entitled to catch exceptions and turn them into its own
                    # "malformed body" answer, and this is not that. It stops
                    # reading either way, and the answer below is ours.
                    overflowed = True
                    return {"type": "http.disconnect"}
            return message

        async def watched_send(message: Message) -> None:
            nonlocal started
            if overflowed and not started:
                return  # Whatever the route made of the truncation, not this.
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, watched_send)
        except Exception:
            # The route tripping over the cut-off body is the expected shape of
            # an overflow, not a fault worth reporting as one.
            if not overflowed:
                raise
        if overflowed and not started:
            await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        megabytes = self.max_bytes // (1024 * 1024)
        response = JSONResponse(
            {"detail": f"That request body is larger than {megabytes} MB"},
            status_code=413,
        )
        await response({"type": "http"}, _no_receive, send)


async def _no_receive() -> Message:
    # A canned response never reads the request; this is here to satisfy the
    # signature.
    return {"type": "http.disconnect"}
