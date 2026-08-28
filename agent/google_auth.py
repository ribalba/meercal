"""Mint the Google refresh token, once, and print the account block for it.

`agent/google.py` can refresh an access token forever, but something has to
hand it the first refresh token, and that is a conversation with a browser
rather than a function call. This is that conversation.

The shape of it is fixed by Google, not by us. Basic auth to their CalDAV
endpoint has been off for years; the out-of-band "paste this code" flow they
used to offer instead was switched off in October 2022. What is left for a
desktop client is a loopback redirect: we listen on a port, send you to
Google, and Google sends the browser back to us with the code. So this file is
a forty-line web server that exists for about one minute.

Two ways the code gets home, running at the same time, because a self-hosted
calendar is as likely to be installed over ssh as on a laptop:

  the browser comes back   the machine with the browser is this machine, the
                           redirect reaches the listener, nothing to type
  you paste the URL        it is not, the browser lands on a dead address, and
                           the address bar now holds the code — paste it here

Everything a human reads goes to stderr; stdout gets the TOML block and
nothing else. That is what lets `meercal.sh google-auth` capture the result
while you still watch the prompts:

    python -m agent.google_auth --client-id … --client-secret …

Run it by hand and stdout is the terminal, so you simply see the block.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import os
import queue
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

from .google import SCOPE, TOKEN_URL, access_token

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# Google lets a *Desktop* client redirect to any port on the loopback address
# without registering it, which is the only reason this works without asking
# you to type a port into the Cloud Console.
DEFAULT_PORT = 8765

# Long enough to find the password manager, create the client, and read the
# consent screen properly; short enough that a forgotten container does not sit
# holding a port all week.
TIMEOUT = 600


def out(msg: str = "") -> None:
    """Human-facing. stderr, always: stdout belongs to the TOML block."""
    print(msg, file=sys.stderr, flush=True)


# --- the browser half ---------------------------------------------------------


_DONE_PAGE = b"""<!doctype html>
<meta charset="utf-8"><title>meercal</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:20vh auto;max-width:26rem;
padding:0 1.5rem;color:#1d1d1f}h1{font-size:1.25rem;margin:0 0 .5rem}
p{color:#555;margin:0}</style>
<h1>%s</h1><p>%s</p>
"""


def _page(title: str, body: str) -> bytes:
    return _DONE_PAGE % (title.encode(), body.encode())


def _handler(state: str, results: "queue.Queue[tuple[str, str]]"):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (params.get("code") or [""])[0]
            error = (params.get("error") or [""])[0]
            got_state = (params.get("state") or [""])[0]

            # Browsers ask for /favicon.ico on their own; that is not an answer.
            if not code and not error:
                self.send_response(404)
                self.end_headers()
                return

            if error:
                self._reply(400, "Google said no", f"It reported: {error}. Nothing was saved.")
                results.put(("error", error))
                return
            # The state is the only thing standing between this listener and any
            # page in your browser that fancies feeding it a code.
            if got_state != state:
                self._reply(400, "That did not come from here", "The state did not match.")
                return
            self._reply(200, "Done.", "meercal has the code. You can close this tab.")
            results.put(("code", code))

        def _reply(self, status: int, title: str, body: str) -> None:
            payload = _page(title, body)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            """Silent: the terminal is holding a conversation."""

    return Handler


# --- the paste half -----------------------------------------------------------


def _code_from_paste(text: str, state: str) -> str:
    """A pasted redirect URL, or a bare code. Empty string if it is neither."""
    text = text.strip().strip('"').strip("'")
    if not text:
        return ""  # a bare Enter is not a mistake worth commenting on
    if "?" in text or text.startswith("http"):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if params.get("error"):
            out(f"  Google reported: {params['error'][0]}")
            return ""
        got_state = (params.get("state") or [""])[0]
        if got_state and got_state != state:
            out("  That URL is from a different attempt; ignoring it.")
            return ""
        return (params.get("code") or [""])[0]
    # Some browsers make selecting the whole address bar hard, so a bare
    # 4/0Ax… pasted on its own is accepted too — but only if it could plausibly
    # be one. Without the length test a stray word typed at this prompt becomes
    # "the code", and the failure surfaces as an opaque complaint from Google
    # three seconds later instead of as "that is not a code" right here.
    if len(text) >= 20 and not any(c.isspace() for c in text):
        return text
    out("  No code in that. Paste the whole address, starting http://127.0.0.1")
    return ""


def _paste_reader(state: str, results: "queue.Queue[tuple[str, str]]") -> None:
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            return  # no stdin to speak of; the listener is still live
        code = _code_from_paste(line, state)
        if code:
            results.put(("code", code))
            return


# --- the flow ----------------------------------------------------------------


def mint(client_id: str, client_secret: str, port: int, bind: str, open_browser: bool) -> str:
    """Walk the OAuth dance and return a refresh token."""
    # PKCE. Google does not force it on a client with a secret, but the secret
    # in a desktop app is not really a secret, and this is six lines.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    # What Google is told, and so what the token request must repeat verbatim.
    # 127.0.0.1 rather than localhost: the two are different strings to Google's
    # exact-match check, and one of them resolves through your hosts file.
    redirect_uri = f"http://127.0.0.1:{port}"
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # offline is what asks for a refresh token at all; consent is what
            # makes Google hand one over on the second and third attempt too,
            # instead of silently reusing a grant it already has.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    results: queue.Queue[tuple[str, str]] = queue.Queue()
    try:
        # 0.0.0.0 by default because the usual caller is a container, where the
        # loopback the browser talks to is the *host's* and arrives over the
        # bridge. The port is published on 127.0.0.1 only, and for one minute.
        server = http.server.HTTPServer((bind, port), _handler(state, results))
    except OSError as exc:
        raise SystemExit(
            f"Cannot listen on {bind}:{port} ({exc}). Something else has that port; "
            f"pass a different one with --port."
        )

    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_paste_reader, args=(state, results), daemon=True).start()

    out("")
    out("Open this in a browser and approve it:")
    out("")
    out(f"  {auth_url}")
    out("")
    out(f"Waiting. If the browser is on this machine it comes back to {redirect_uri}")
    out("on its own. If it is not, the page will fail to load — copy the whole")
    out("address it failed on out of the address bar and paste it here:")
    out("")

    if open_browser:
        webbrowser.open(auth_url)

    try:
        kind, value = results.get(timeout=TIMEOUT)
    except queue.Empty:
        raise SystemExit(f"Nothing came back within {TIMEOUT // 60} minutes. Nothing was changed.")
    finally:
        server.shutdown()
    if kind != "code":
        raise SystemExit(f"Google refused: {value}")

    out("")
    out("Got the code. Trading it for a refresh token…")
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": value,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Google refused the code: {response.text[:400]}")
    payload = response.json()
    token = payload.get("refresh_token", "")
    if not token:
        raise SystemExit(
            "Google returned an access token but no refresh token. That happens when "
            "the client has been approved before; remove meercal at "
            "https://myaccount.google.com/permissions and run this again."
        )
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.google_auth",
        description="Mint the Google refresh token for a [[agent.account]] block.",
    )
    # Secrets come by environment rather than argv, because argv is readable by
    # every process on the machine and the environment of a container is not.
    parser.add_argument("--client-id", default=os.environ.get("MEERCAL_GOOGLE_CLIENT_ID", ""))
    parser.add_argument(
        "--client-secret", default=os.environ.get("MEERCAL_GOOGLE_CLIENT_SECRET", "")
    )
    parser.add_argument("--username", default="", help="the Google address the calendars are on")
    parser.add_argument("--label", default="Google", help="what to call the account in meercal")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument("--bind", default="0.0.0.0", help="listen address (default 0.0.0.0)")
    parser.add_argument("--open", action="store_true", help="try to open the browser here")
    args = parser.parse_args(argv)

    client_id = args.client_id.strip()
    client_secret = args.client_secret.strip()
    username = args.username.strip()

    if not client_id or not client_secret:
        out("")
        out("A Google OAuth client of your own, from the Cloud Console:")
        out("  1. console.cloud.google.com → a project, any project")
        out("  2. APIs & Services → Library → enable the CalDAV API")
        out("  3. Google Auth Platform → Audience → External, then Publish app")
        out("  4. Google Auth Platform → Clients → Create client → Desktop app")
        out("")
        out("Publish it rather than leaving it on Testing: a project still in")
        out("Testing issues refresh tokens that expire after seven days, which")
        out("means the calendar stops syncing every week. Published-but-unverified")
        out("is fine for an app only you use — Google warns at the consent screen")
        out("and Advanced -> Go to Meercal goes through.")
        out("")
        try:
            client_id = client_id or input("Client ID: ").strip()
            client_secret = client_secret or input("Client secret: ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return 1
    if not client_id or not client_secret:
        out("A client ID and secret are both needed. Nothing was changed.")
        return 1
    if not username:
        try:
            username = input("Your Google address: ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return 1

    token = mint(client_id, client_secret, args.port, args.bind, args.open)

    # Prove it before printing it. This is the exact call the agent makes every
    # hour, so a token that survives it is a token that will keep working.
    out("Checking that it refreshes…")
    access_token(client_id, client_secret, token)
    out("It does.")
    out("")
    out("Add this to meercal.toml, then restart:")
    out("")

    print("[[agent.account]]")
    print(f'label = "{args.label}"')
    print('kind = "google"')
    print(f'username = "{username}"')
    print(f'client_id = "{client_id}"')
    print(f'client_secret = "{client_secret}"')
    print(f'refresh_token = "{token}"')

    out("")
    out("`meercal.sh test` will then say whether Google actually lets it in.")
    out("")
    out("If the Cloud Console project is still on Testing rather than published,")
    out("this token stops working in seven days. That is Google's rule for")
    out("unverified apps, and nothing here can work around it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
