<p align="center">
  <img src="app/static/img/logo.png" width="180" alt="meercal logo" />
</p>

<h1 align="center">meercal</h1>

<p align="center">The meercal calendar, for people who have too many calendars</p>

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
hidden ones included · clashes between calendars marked, not left to be noticed ·
a person against the events you are not on your own at, invitations included ·
**reminders** on your desktop, your phone or an actual phone call, with rules written in the
same filter language and a mute per event · full
keyboard control · light + dark, following the system or pinned · optional **meerail**
integration: invite from the people you actually write to.

It splits into two pieces, for the same reason meerail does:

- **`meercal-agent`**: runs on your machine and owns the whole write path: it speaks CalDAV
  to iCloud and friends, parses and expands what it finds, and writes it into Postgres. Your
  calendar passwords never leave the host.
- **`meercal-server`**: the web layer in Docker: FastAPI plus the UI. It only reads the
  database and enqueues your actions; it never fetches a calendar and holds no credentials.
- **`core`**: the library both import: models, parsing, recurrence expansion, ingest.

## Background

**Long events are the hard part, and every calendar draws them badly.** A month grid wraps
time every seven days, so a fortnight-long trip becomes two bars on two lines with nothing to
say they are the same thing; a week grid pushes it into an all-day strip that scrolls out of
sight. The thing you are actually in the middle of ends up being the hardest thing on the
screen to see. The Ribbon does not wrap: days run down the page, long events are continuous
bars in a rail beside them, packed into parallel lanes the way a commit graph packs branches,
and each bar's label is sticky: it rides down beside whatever day you are reading and says
*day 4 of 19*.

**Many calendars, not one.** Work, family, the school, an on-call rota, two clients, a
conference feed. meercal treats that as the normal case: runs of empty days collapse to a
single line so a month of several calendars fits on a screen, calendars group into **sets**
you switch with a number key, alt-click solos one the way a layer solo works in an editor,
and hiding a calendar never hides it from search.

**Expansion, materialised.** Recurrence is expanded into rows over a rolling horizon, so
drawing a fortnight is one index scan whatever the number of calendars, rather than running
twenty rule engines per repaint, which is exactly what makes other clients slow in the case
this program exists for.

**Postgres as the store, not a cache.** Years of your time in a real database, with the
original `VEVENT` text kept alongside, so `make psql` can answer questions no calendar app
exposes, and an edit patches the server's own iCalendar rather than rewriting it from a model
that does not know about the alarm your phone set.

## Requirements

| | |
| --- | --- |
| **Docker** | Engine 24+ with the Compose v2 plugin. Runs the web layer and its Postgres. |
| **Python** | 3.11+ on the host, for the agent, which runs outside Docker on purpose. |
| **Calendar access** | An app-specific password for iCloud (appleid.apple.com; your normal password will not work), or any CalDAV account. Google needs an OAuth client; a secret `.ics` address works read-only with no credentials at all. |
| **Disk** | Small. A calendar is kilobytes per event; the expansion table is the bulk of it, and it is bounded by the horizon. |

## Install: the quick way

No clone, no build, no Python on your machine. One script that asks what it needs, writes a
configuration, and runs the published containers.

```bash
curl -fsSL https://raw.githubusercontent.com/ribalba/meercal/main/meercal.sh -o meercal.sh
bash meercal.sh
```

It checks Docker is there, asks where your calendars live (iCloud, CalDAV, a published `.ics`
feed, Google), picks a free port, pulls `ribalba/meercal-{server,agent}` from Docker Hub and
starts them. Everything it writes lives in `~/.meercal` (override with `MEERCAL_HOME`); your
events live in a Docker volume.

Afterwards:

```bash
bash meercal.sh status      # containers, version, and whether the accounts are syncing
bash meercal.sh logs agent  # watch the first sync work through your calendars
bash meercal.sh test        # check every account, change nothing
bash meercal.sh sync        # run one pass now
bash meercal.sh demo        # fill it with demo calendars worth looking at
bash meercal.sh google-auth # mint a Google refresh token and add the account
bash meercal.sh update      # pull the newest release and restart
bash meercal.sh config      # edit meercal.toml, then restart
bash meercal.sh backup      # dump the database
bash meercal.sh help        # everything else
```

Windows: run it inside WSL2 or Git Bash, with Docker Desktop running.

Unlike meerail, the **agent is a container here too**. meerail has to run its agent on the
host because Proton Bridge listens on the host's loopback; meercal's agent talks to CalDAV
servers out on the internet, so there is nothing on your machine it needs to reach. Your
calendar passwords still stay put: they are in `~/.meercal/meercal.toml`, mode 0600, mounted
read-only into the two containers that read it, which is also why those containers run as
you rather than as root.

### On a platform that writes the file for you

The agent does not merely prefer mode 0600, it refuses to sync without it, and with
`restart: unless-stopped` in front of it that refusal is a loop:

```
meercal: /app/meercal.toml is readable by other users and holds passwords.
```

Coolify, Dokku and a Kubernetes config map all write their file mounts root-owned and 0644,
and write them again on every deploy, so a `chmod 600` on the host holds until the next one.
The durable fix is to mount a path the platform does not manage, made once by hand and owned
by the uid the container runs as:

```bash
install -D -m 600 -o 10001 -g 10001 meercal.toml /data/meercal/meercal.toml
#   then, in the compose:  - /data/meercal/meercal.toml:/app/meercal.toml:ro
```

Create it before the first deploy: Docker makes a *directory* at a bind mount whose source
does not exist, and the agent then finds no configuration at all. If you would rather leave
the file where the platform put it, `MEERCAL_INSECURE_CONFIG=1` turns the refusal into one
warning line at startup. That is a statement about the host — every user on it can read your
calendar passwords — which is a fair trade on a VPS whose only other user is root, and no
trade at all on a machine other people have accounts on.

The other thing such a platform rewrites is the network. The compose files give meercal's
Postgres the network alias `meercal-db`, so that a server attached to meerail's network as
well cannot mistake one stack's database for the other's. An alias belongs to a network, and
a platform that attaches every service to a network of its own writes it out again without
one — at which point the name resolves nowhere and the server exits before it serves a
request:

```
meercal: database not reachable at 'postgresql+psycopg://meercal:...@meercal-db:5432/meercal':
  failed to resolve host 'meercal-db': [Errno -3] Temporary failure in name resolution
```

Nothing in the deployed files depends on that alias any more: both `DATABASE_URL`s address
the database as `db`, the service name, which is the one name every platform keeps. If you
are carrying an older copy of a compose file, change `@meercal-db:5432` to `@db:5432` in
both of them. The alias survives for the one caller that needs it, in
`docker-compose.meerail.yml`, where `db` really is ambiguous.

## Install: from a checkout

```bash
git clone https://github.com/ribalba/meercal
cd meercal
cp meercal.example.toml meercal.toml && chmod 600 meercal.toml
make up          # postgres + server + agent  ->  http://127.0.0.1:8010
```

Nothing to look at yet. Either add an account (below), or fill it with a week worth looking
at:

```bash
make venv
make seed        # seven calendars, a 19-day trip, an on-call week, a double booking
```

### Which zone it draws in

Everything is stored in UTC and drawn in one zone, decided once on the server, so that no two
clients can disagree about what a time means. That zone is `server.timezone` in `meercal.toml`,
and it defaults to `"system"` — the machine the server runs on.

In Docker that machine is the *container*, whose own zone is UTC. So compose hands it yours:

```bash
cp .env.example .env      # then set TZ=Europe/Berlin, or whatever you are in
```

`meercal.sh` fills that in from the host it installs on, and `make up` reads it from `.env`.
If the times come out an hour or two off, that is what to check first — or name the zone in
`meercal.toml` outright, which nothing can misread. **The calendar tells you either way**: when
the zone it draws in is not the zone your browser is in, the toolbar says so, because a
calendar that is wrong by a constant offset still looks exactly like a calendar.

All-day events are the exception, in every direction: they are dates, not instants, and never
move between zones.

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

(In a `meercal.sh` install the agent is already running as a container; `meercal.sh test` and
`meercal.sh sync` are the same two commands.)

`make agent-test` is the first thing to run when a calendar is not appearing: it reports, per
account, whether discovery worked and which calendars it found.

### Google

Basic auth to Google's CalDAV endpoint has been off for years, so an app password does not
open it the way it opens Gmail for meerail. Two ways in:

- **The secret `.ics` address** (Calendar settings → *Integrate calendar* → *Secret address in
  iCal format*) as an `ics` account. Read-only, no credentials, works today.
- **OAuth**, for read and write: create a Desktop client in the Google Cloud Console (enable
  the *CalDAV API*; under *Google Auth Platform → Audience* choose External and then
  **Publish app**), then mint a refresh token with `meercal.sh google-auth`, or
  `python -m agent.google_auth` from a checkout. It
  sends you to Google, catches the redirect on `127.0.0.1`, and prints the `[[agent.account]]`
  block — `client_id`, `client_secret`, `refresh_token`, with `kind = "google"`. In a
  `meercal.sh` install that block is appended to `meercal.toml` for you. If the browser is on
  a different machine than the install, the page it lands on will fail to load; paste that
  address back at the prompt and the code comes home that way. The rest is ordinary CalDAV
  with a bearer token; see `agent/google.py`.

  Publishing the project is not optional busywork. Google issues refresh tokens that
  [expire after seven days](https://developers.google.com/identity/protocols/oauth2#expiration)
  to any project whose publishing status is still *Testing*, so an unpublished client means the
  calendar goes quiet every week. Published-but-*unverified* is the correct state for something
  only you run: Google shows an "unverified app" warning at the consent screen, *Advanced → Go
  to Meercal* goes through, and the token then lasts until it is revoked. Verification only
  matters if you intend to hand the client to strangers.

### Import an .ics file

Drop a calendar file anywhere on the window. meercal reads it, tells you what is in it — how
many events, which dates, what the file calls itself — and asks which calendar it goes in.
There is an **Import…** link under the calendar list for the same thing when the file is not
somewhere you can drag it from.

The default is a **new calendar**, which lands under an *Imported* heading in the sidebar and
stays local: no server, nothing to sync, one tickbox away from hidden if it was a mistake.
Pick one of your own calendars instead and the events go there, and a calendar with a server
behind it has them queued for the agent the same way an edit here is — the dialog says so, and
they are on the server after the next pass.

Importing the same file twice **updates rather than duplicates**: events are matched by their
UID, so re-importing a corrected export does the obvious thing. Events outside the sync
horizon are stored but not drawn until it reaches them, and the dialog says how many of those
there are rather than letting them look lost.

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

The **week and day grids are one continuous scroll**, like the Ribbon: consecutive weeks are
drawn one under the other, so eleven at night on Sunday is followed by midnight on Monday.
Each week's day labels are sticky and are pushed up by the next week's as you scroll into it,
the date in the toolbar follows the scroll, and the loaded window slides as you go. Nothing
pages and nothing swaps under a header that stayed still. The arrows and `g` scroll there too,
so a week boundary looks the same however you cross it.

The **mouse wheel pages** in the month and year views, which are pages.

In the week and day grids you can **draw on the grid**: sweep empty space to write a new event
over the hours you swept, drag an event to move it, and drag either end of one to change just
that end. Everything snaps to the quarter hour, the view follows a drag that reaches its edge,
and Escape puts it back. Dragging an event past the bottom of its week drops it in the next
one. Read-only calendars have no handles, and moving something that repeats says so before it
changes every occurrence.

### Places you keep typing

Half the locations in a calendar are the same handful: the office, that room, the same
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

A set is a named group of calendars with a number key on it: the answer to having twenty of
them. Click one to apply it, the pencil to change it: rename, move its key, tick what belongs
in it. A key belongs to one set, so giving `3` to a second set takes it off the first rather
than leaving two that both answer to it. Sets are listed in key order, which puts `0` at the
top.

## Filtering and search

The bar above the calendar **filters the view you are in**: words are ANDed, a quoted phrase
matches whole, and the filters are the nouns of a calendar:

```
standup cal:work with:anna in:berlin is:span is:recurring is:free
```

Tick **Regex** and the pattern goes to Postgres as a POSIX regular expression (`~*`) against a
trigram-indexed column, so `standup|jour fixe` costs what a word costs. Press **Enter** and
the same query becomes a search across *every* calendar, including the ones you have switched
off; hiding is about the drawing, never about the data.

## Reminders

Reminders are the one thing a calendar does unprompted, so meercal puts them where the
credentials already are: **the agent**. A container cannot show you a desktop notification and
should not hold a Twilio token. The scheduler runs beside the sync loop, or on its own, if
the machine you sit at is not the machine that syncs.

Four places a reminder can go, and adding a fifth is one file:

| | |
| --- | --- |
| `desktop` | `notify-send` on this machine. `urgency = "critical"` means it stays on screen until dismissed, which is the only setting that makes a reminder a reminder. |
| `ntfy` | A push to your phone, over [ntfy](https://ntfy.sh) or your own server. |
| `twilio` | An actual phone call. The TwiML goes inline in the request, so this needs **no public webhook**, and nothing of yours is exposed. Or `mode = "sms"`. |
| `app` | A notification from the meercal window, while it is open. The one that works with no agent running. |
| `command` / `webhook` | The escape hatches: run a program, or POST some JSON. Home Assistant, a lamp, `signal-cli`. |

### A rule is a filter string

There is no second query language. A rule is something you could type into the filter bar,
plus a lead time:

```toml
[[reminders.rule]]
name = "work, on the phone too"
match = "cal:work is:busy"
except = "Lunch"                # a shape of event this rule should skip
lead = ["1h", "10m"]            # a list arms one reminder per entry
channels = ["desk", "phone"]

[[reminders.rule]]
name = "birthdays, the evening before"
match = "is:allday cal:birthdays"
at = "-1d 18:00"                # an absolute wall clock: an all-day event has no clock
channels = ["phone"]
```

`lead = "valarm"` defers to the alarm the calendar server already carries, so meercal agrees
with your phone instead of telling you the same thing five minutes later.

### The lunch problem

A rule is a statement about a *kind* of event, and there is always an event that is the wrong
kind. A daily "Lunch" matches `cal:work is:busy` as squarely as a client meeting does, and no
filter string can see the difference. The difference is that you know what lunch is.

So the **event has the last word**. The bell in the event panel sets each channel to one of
three things, and *auto is not the same as on*:

| | |
| --- | --- |
| **auto** | Whatever the rules say. The default, and it follows a rule you write next month. |
| **on** | Always, whether or not a rule matched. |
| **off** | Never, including against rules that do not exist yet. |

That third state is the point. "Never call me about lunch" becomes one bit that stays true
forever, and the desktop popup still arrives, because only the call was muted. On a repeating
event the panel asks whether you mean all of them or just this one.

Resolved in this order, and `off` beats `on`:

1. this occurrence · 2. the whole series · 3. the event's own VALARM · 4. matching rules ·
5. nothing fires

Quiet hours and daily caps sit above all five: a hand-set **on** does not buy a way past
`max_per_day`, because that setting exists to stop exactly the accident a hand-set override is
most likely to cause.

### Two commands worth knowing

```bash
make remind-test            # one real notification through every channel
make remind-next            # what would fire in the next 24 hours, muted ones included
```

`--test` verifies the Twilio credentials **without placing a call**: the moment to find out a
token is wrong is not the morning you miss the appointment. `--next` resolves the same chain
the scheduler does and prints muted reminders too, so *"why didn't it ring"* has an answer
that is one command long:

```
Mon 16:34  Lunch            desk     · muted on the whole series  [muted]
Mon 17:29  Zahnarzt         desk     · work, on the phone too
Mon 17:29  Zahnarzt         phone    · work, on the phone too
```

### Run it as a user service, not a system one

`notify-send` needs the session bus. Under a systemd **system** unit there is no
`DBUS_SESSION_BUS_ADDRESS`, and every desktop reminder fails into a log nobody reads while the
calendars carry on syncing perfectly. `make remind-test` refuses to pass in that state rather
than letting you discover it later. There is a user unit in
[`contrib/meercal-agent.service`](contrib/meercal-agent.service):

```bash
cp contrib/meercal-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now meercal-agent
```

### One privacy note

A public ntfy topic is readable by anyone who guesses its name, and what travels over it is
the titles of your appointments. Make the topic long and random, keep the token in the
environment rather than in `meercal.toml`, and use `detail` to decide how much leaves the
machine: `full`, `title`, or `none`, where `none` sends "a reminder" and you open the app to
see what it was. Self-hosting ntfy is the real answer; `server` is one line.

## The desktop app

An Electron shell, the same one meerail and meerato have:

```bash
make desktop                                   # npm install && npm start
cd electron && make distinstall                # build and register it with the desktop
```

It is a window around the same web app, so nothing moves, but it adds the two
things a browser tab cannot. It reports the window's **focus** to the page,
which is how meercal knows it is behind another window rather than merely
hidden, and stands its polling down until you come back (then reloads, because
the agent has been syncing all along). And it **spell checks** the event
panel's title, location and notes.

`MEERCAL_URL` points it somewhere other than `http://localhost:8010`; see
[`electron/README.md`](electron/README.md) for the installers and what
`distinstall` puts where.

## Updating

The server asks github once a day whether a newer `VERSION` is on main, and shows a quiet
strip at the foot of the sidebar if there is: dismissible, and dismissing pins that version
so the next release says its piece and this one stays quiet. It is the only outbound request
meercal makes; `update_check = false` in `meercal.toml` means it makes none at all.

Taking the update:

```bash
bash meercal.sh update    # a meercal.sh install: pulls the new images and restarts
```

```bash
git pull && make up       # a checkout: rebuilds and restarts
```

Either way the database migrates itself on first boot, and your events are in a Docker volume
that neither path touches. `bash meercal.sh backup` first if you would rather not find out.

## Working with meerail

Set `[meerail] database_url` to your [meerail](https://github.com/ribalba/meerail) database
and the invite field autocompletes from the people you actually correspond with: meerail
builds that address book from every message it holds, ranked by how often. Pick one and it
becomes a bubble under the name meerail knows them by. It is read-only: meercal never writes
to your mail.

## On a phone

The same app, laid out for the width: the sidebar becomes a drawer behind the ☰ button, the
view switch gets a row of its own rather than falling off the end of the toolbar, and Create
is a floating button. The Ribbon is the view that gains most from a narrow screen (it is a
single column by nature), and the week grid scrolls sideways rather than squeezing seven
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
| `events` | One `VEVENT` as the server sent it: the rule, not the instances |
| `occurrences` | The expansion: one row per appearance, over a rolling horizon |
| `calendar_sets` | Named groups of calendars, with a number key |
| `pending_actions` | Changes made in the UI, waiting for the agent to push them |

## Development

`docker compose up` from a checkout builds and runs the same three containers an install
gets, so what you are testing has the shape of what you ship. It needs `MEERCAL_UID`/`GID` in
`.env` (see `.env.example`) to read your mode-0600 `meercal.toml`, and it takes a project name
of its own, `meercal-dev`, so that a checkout in a directory called `meercal` cannot collide
with the install `meercal.sh` puts in `~/.meercal` — same derived name, shared volume, and a
database that answers with the wrong password.

Running a piece of it natively is still the faster loop, and the only way to reach the host:
OAuth (`python -m agent.google_auth`) and desktop reminders need your session, not a
container.

```bash
make venv                  # .venv with both requirement sets
make infra                 # just Postgres
make dev                   # uvicorn --reload on :8000
make agent                 # the connector, outside Docker
make test-db && make test  # the suite, against a throwaway database

tools/caldav_test_server.sh start   # a real Radicale to test the agent against
make test
tools/caldav_test_server.sh stop
```

The tests that need Postgres skip without `MEERCAL_TEST_DB`, and the CalDAV ones skip without
a server, so a bare `pytest` still runs everything that does not.

### Releasing

```bash
make images     # build ribalba/meercal-{server,agent} for this machine
make hub-up     # run the published-image stack from what you just built
make push       # build for linux/amd64 + linux/arm64 and push to Docker Hub
```

`VERSION` is the single source of the number: it tags both images, stamps their OCI labels,
and is what an install compares itself against to notice an update. `make push` gives each
image `:$(VERSION)` and `:latest` in one command, so the two tags cannot end up pointing at
different builds, which is the failure that has somebody debugging a version they are not
running. It needs `docker login` first.

The website that fronts this is in [`website/`](website/); its screenshots are captured from
a running instance rather than mocked.

## Status

0.1.0, and honest about it. What works: sync (CalDAV incremental with sync tokens, `.ics`
feeds), the write path (create, edit, delete, queued and pushed), all four views, sets and
solo, filter and search, the meerail address book.

Not there yet:

- **Editing one instance of a repeat.** An edit changes the whole series, and the panel says
  so. Writing an override with a `RECURRENCE-ID` is the next thing.
- **Invitations.** Everyone invited is a bubble on the event, carrying whatever the server
  last said about them: a check for an acceptance, a struck-through name for a no, and an ×
  to take someone off again. What is missing is the mail. meercal does not send or process
  iTIP itself, so whether an invitation reaches anybody is up to the calendar server: one
  that does RFC 6638 scheduling sends it on the PUT, and a plain store just keeps the text.
- **Drag to move or resize.** Everything goes through the event panel for now.

## License

AGPL-3.0. See [LICENSE](LICENSE).
