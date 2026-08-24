<p align="center">
  <img src="app/static/img/logo.png" width="180" alt="meercal logo" />
</p>

<h1 align="center">meercal</h1>

<p align="center">The meercal calendar — for people who have too many calendars</p>

---

meercal is a fast, self-hosted calendar client for **Apple Calendar (iCloud), CalDAV, Google
and plain `.ics` feeds**, built around one idea the usual grid gets wrong: **time does not
wrap every seven days.** Its own view, the **Ribbon**, runs days down the page continuously,
so a three-week event is one unbroken bar you can follow instead of three fragments you have
to re-recognise on every line. Everything lives in **PostgreSQL**. Runs on Linux, macOS and
Windows.

**Features:** the Ribbon · week / month / day the way you already know them, with long events
drawn continuously · **calendar sets** on number keys and alt-click **solo**, for the twenty
calendars problem · iCloud, CalDAV, Google and `.ics` · a filter bar that types like meerail's
search (`cal:family with:anna is:span`, POSIX regex optional) · search across every calendar,
hidden ones included · clashes between calendars marked, not left to be noticed · full
keyboard control · light + dark, following the system or pinned · optional **meerail**
integration: invite from the people you actually write to.

It splits into two pieces, for the same reason meerail does:

- **`meercal-agent`** — runs on your machine and owns the whole write path: it speaks CalDAV
  to iCloud and friends, parses and expands what it finds, and writes it into Postgres. Your
  calendar passwords never leave the host.
- **`meercal-server`** — the web layer in Docker: FastAPI plus the UI. It only reads the
  database and enqueues your actions; it never fetches a calendar and holds no credentials.
- **`core`** — the library both import: models, parsing, recurrence expansion, ingest.

## Background

**Long events are the hard part, and every calendar draws them badly.** A month grid wraps
time every seven days, so a fortnight-long trip becomes two bars on two lines with nothing to
say they are the same thing; a week grid pushes it into an all-day strip that scrolls out of
sight. The thing you are actually in the middle of ends up being the hardest thing on the
screen to see. The Ribbon does not wrap: days run down the page, long events are continuous
bars in a rail beside them, packed into parallel lanes the way a commit graph packs branches,
and each bar's label is sticky — it rides down beside whatever day you are reading and says
*day 4 of 19*.

**Many calendars, not one.** Work, family, the school, an on-call rota, two clients, a
conference feed. meercal treats that as the normal case: runs of empty days collapse to a
single line so a month of several calendars fits on a screen, calendars group into **sets**
you switch with a number key, alt-click solos one the way a layer solo works in an editor,
and hiding a calendar never hides it from search.

**Expansion, materialised.** Recurrence is expanded into rows over a rolling horizon, so
drawing a fortnight is one index scan whatever the number of calendars — rather than running
twenty rule engines per repaint, which is exactly what makes other clients slow in the case
this program exists for.

**Postgres as the store, not a cache.** Years of your time in a real database, with the
original `VEVENT` text kept alongside — so `make psql` can answer questions no calendar app
exposes, and an edit patches the server's own iCalendar rather than rewriting it from a model
that does not know about the alarm your phone set.

## Requirements

| | |
| --- | --- |
| **Docker** | Engine 24+ with the Compose v2 plugin. Runs the web layer and its Postgres. |
| **Python** | 3.11+ on the host, for the agent — which runs outside Docker on purpose. |
| **Calendar access** | An app-specific password for iCloud (appleid.apple.com — your normal password will not work), or any CalDAV account. Google needs an OAuth client; a secret `.ics` address works read-only with no credentials at all. |
| **Disk** | Small. A calendar is kilobytes per event; the expansion table is the bulk of it, and it is bounded by the horizon. |

## Quick start

```bash
git clone https://github.com/ribalba/meercal
cd meercal
cp meercal.example.toml meercal.toml && chmod 600 meercal.toml
make up          # server + postgres  ->  http://127.0.0.1:8010
```

Nothing to look at yet. Either add an account (below), or fill it with a week worth looking
at:

```bash
make venv
make seed        # seven calendars, a 19-day trip, an on-call week, a double booking
```

### Add your calendars

One `[[agent.account]]` block per account in `meercal.toml`:

```toml
[[agent.account]]
label = "Family"
kind = "icloud"                       # apple: needs an app-specific password
username = "you@icloud.com"
password = "abcd-efgh-ijkl-mnop"

[[agent.account]]
label = "Nextcloud"
kind = "caldav"
url = "https://cloud.example.com/remote.php/dav"
username = "didi"
password = "…"
only = "Work|Releases"                # optional: regex over the calendar name

[[agent.account]]
label = "School holidays"
kind = "ics"                          # a read-only feed, no credentials
url = "https://example.com/holidays.ics"
```

Then run the agent:

```bash
make agent-test   # prove every connection, change nothing
make agent        # sync now, and every [agent] interval after
```

`make agent-test` is the first thing to run when a calendar is not appearing: it reports, per
account, whether discovery worked and which calendars it found.

### Google

Basic auth to Google's CalDAV endpoint has been off for years, so an app password does not
open it the way it opens Gmail for meerail. Two ways in:

- **The secret `.ics` address** (Calendar settings → *Integrate calendar* → *Secret address in
  iCal format*) as an `ics` account. Read-only, no credentials, works today.
- **OAuth**: create a Desktop client in the Google Cloud Console, then put `client_id`,
  `client_secret` and `refresh_token` in the account block with `kind = "google"`. The rest is
  ordinary CalDAV with a bearer token — see `agent/google.py`.

## The views

| Key | View | |
| --- | --- | --- |
| `r` | **Ribbon** | Continuous days, long events unbroken in the rail, quiet days collapsed |
| `w` | Week | The time grid, with long events running across it |
| `m` | Month | Six weeks, with bars that keep their lane when the grid wraps |
| `d` | Day | One column |
| `y` | Year | Twelve months, under a band of every long event in the year |

Other keys: `t` today · `←`/`→` back and forward · `g` then `1`–`12` jump to a month
(a leading `1` waits a moment for its second digit, so `g 9` is instant and `g 12` works; a
hint at the foot of the window says what it is waiting for) · `0`–`9` a calendar set (`0`
means everything, with or without a set on it) · `c` new event · `/` filter · `q` expand the
quiet days · `.` sync now · `?` the rest.

Clicking an empty day in the Ribbon starts an event on it, the way double-clicking the grid
does in the other views. The cheat sheet in the sidebar is generated from the table
that binds them, so it cannot drift.

The **mouse wheel pages** in every view but the Ribbon — which is one continuous scroll and
has nowhere to page to. In the week and day grids it scrolls the hours first and only changes
the date once it runs out of them, so neither gesture costs the other.

### Places you keep typing

Half the locations in a calendar are the same handful — the office, that room, the same
meeting link. Put them in `meercal.toml` and they are offered as chips under the event
panel's **Where** field:

```toml
[server.places]
"Office" = "Ritterstr. 12, 10969 Berlin"
"Meet" = "https://meet.example.com/abc-defg"
"Kita" = "Kita Sonnenschein, Reichenberger Str. 1"
```

The key is the label on the chip, the value is what goes into the field; they are offered in
the order written. Clicking the chip that is already in the field clears it again.

### Sets

A set is a named group of calendars with a number key on it — the answer to having twenty of
them. Click one to apply it, the pencil to change it: rename, move its key, tick what belongs
in it. A key belongs to one set, so giving `3` to a second set takes it off the first rather
than leaving two that both answer to it. Sets are listed in key order, which puts `0` at the
top.

## Filtering and search

The bar above the calendar **filters the view you are in** — words are ANDed, a quoted phrase
matches whole, and the filters are the nouns of a calendar:

```
standup cal:work with:anna in:berlin is:span is:recurring is:free
```

Tick **Regex** and the pattern goes to Postgres as a POSIX regular expression (`~*`) against a
trigram-indexed column, so `standup|jour fixe` costs what a word costs. Press **Enter** and
the same query becomes a search across *every* calendar, including the ones you have switched
off — hiding is about the drawing, never about the data.

## The desktop app

An Electron shell, the same one meerail and meerato have:

```bash
make desktop                                   # npm install && npm start
cd electron && make distinstall                # build and register it with the desktop
```

It is a window around the same web app, so nothing moves — but it adds the two
things a browser tab cannot. It reports the window's **focus** to the page,
which is how meercal knows it is behind another window rather than merely
hidden, and stands its polling down until you come back (then reloads, because
the agent has been syncing all along). And it **spell checks** the event
panel's title, location and notes.

`MEERCAL_URL` points it somewhere other than `http://localhost:8010`; see
[`electron/README.md`](electron/README.md) for the installers and what
`distinstall` puts where.

## Working with meerail

Set `[meerail] database_url` to your [meerail](https://github.com/ribalba/meerail) database
and the attendee field autocompletes from the people you actually correspond with — meerail
builds that address book from every message it holds, ranked by how often. It is read-only:
meercal never writes to your mail.

## On a phone

The same app, laid out for the width: the sidebar becomes a drawer behind the ☰ button, the
view switch gets a row of its own rather than falling off the end of the toolbar, and Create
is a floating button. The Ribbon is the view that gains most from a narrow screen — it is a
single column by nature — and the week grid scrolls sideways rather than squeezing seven
columns into forty pixels each.

## Architecture

```
  iCloud / CalDAV / Google / .ics
              │
              ▼
     meercal-agent  (your machine, holds the credentials)
              │  writes
              ▼
        PostgreSQL  ◀──── reads ────  meercal-server (Docker)  ◀── browser
              ▲                                │
              └──────── queue: pending_actions ┘
```

The two halves share nothing but the database. The agent writes; the app reads and queues.
There is no code path in the web layer that could send a credential anywhere, because it does
not have one.

| Table | What it holds |
| --- | --- |
| `accounts`, `calendars` | Where calendars come from, what colour they are, what is drawn |
| `events` | One `VEVENT` as the server sent it — the rule, not the instances |
| `occurrences` | The expansion: one row per appearance, over a rolling horizon |
| `calendar_sets` | Named groups of calendars, with a number key |
| `pending_actions` | Changes made in the UI, waiting for the agent to push them |

## Development

```bash
make venv                  # .venv with both requirement sets
make infra                 # just Postgres
make dev                   # uvicorn --reload on :8000
make test-db && make test  # the suite, against a throwaway database

tools/caldav_test_server.sh start   # a real Radicale to test the agent against
make test
tools/caldav_test_server.sh stop
```

The tests that need Postgres skip without `MEERCAL_TEST_DB`, and the CalDAV ones skip without
a server — so a bare `pytest` still runs everything that does not.

The website that fronts this is in [`website/`](website/); its screenshots are captured from
a running instance rather than mocked.

## Status

0.1.0, and honest about it. What works: sync (CalDAV incremental with sync tokens, `.ics`
feeds), the write path (create, edit, delete, queued and pushed), all four views, sets and
solo, filter and search, the meerail address book.

Not there yet:

- **Editing one instance of a repeat.** An edit changes the whole series, and the panel says
  so. Writing an override with a `RECURRENCE-ID` is the next thing.
- **Invitations.** Attendees are stored and written, but meercal does not send or process
  iTIP mail yet.
- **Google OAuth** is implemented but has not been run against a live account.
- **Drag to move or resize.** Everything goes through the event panel for now.

## License

AGPL-3.0. See [LICENSE](LICENSE).
