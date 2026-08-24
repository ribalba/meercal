#!/usr/bin/env bash
#
# meercal — install and run the whole thing from prebuilt containers.
#
#   curl -fsSL https://raw.githubusercontent.com/ribalba/meercal/main/meercal.sh -o meercal.sh
#   bash meercal.sh
#
# One file. It asks what it needs, writes a configuration, pulls the images from
# Docker Hub and starts them. No clone, no build, no Python on the host, and the
# same commands on Linux, macOS and Windows (WSL2 or Git Bash).
#
# What it creates, all under ~/.meercal:
#
#   meercal.toml        your configuration, mode 0600 — it holds calendar passwords
#   .env                ports, database credentials and the image tag, read by compose
#   docker-compose.yml  fetched from the release you installed
#
# Your calendars live in a Docker volume (meercal-db), not in that directory.
#
# The developer path — clone the repo, `make up`, the agent from a checkout — is
# untouched and documented in README.md. This is the other one: for someone who
# wants their calendar, not a checkout.

set -euo pipefail

# --- where everything lives ---------------------------------------------------

MEERCAL_HOME="${MEERCAL_HOME:-$HOME/.meercal}"
CONFIG_FILE="$MEERCAL_HOME/meercal.toml"
ENV_FILE="$MEERCAL_HOME/.env"
COMPOSE_FILE="$MEERCAL_HOME/docker-compose.yml"

# Where an upgrade and the version pin come from. Overridable so a fork — or a
# test of an unreleased branch — can point the whole script elsewhere.
REPO="${MEERCAL_REPO:-ribalba/meercal}"
RAW_BASE="${MEERCAL_RAW_BASE:-https://raw.githubusercontent.com/$REPO/main}"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- output -------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; BLU=$'\033[34m'; R=$'\033[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; R=""
fi

say()   { printf '%s\n' "$*"; }
info()  { printf '%s\n' "  $*"; }
ok()    { printf '%s✓%s %s\n' "$GRN" "$R" "$*"; }
warn()  { printf '%s!%s %s\n' "$YEL" "$R" "$*"; }
die()   { printf '%s✗ %s%s\n' "$RED" "$*" "$R" >&2; exit 1; }
head1() { printf '\n%s%s%s\n' "$B" "$*" "$R"; }
rule()  { printf '%s%s%s\n' "$DIM" "────────────────────────────────────────────────────────" "$R"; }

# --- prompts ------------------------------------------------------------------
#
# Every answer is read from /dev/tty rather than stdin. Without that, the
# `curl … | bash` form of this script would find its own body on stdin and
# "answer" every question with a line of shell.

need_tty() {
  [ -r /dev/tty ] || die "This needs a terminal. Download the script and run: bash meercal.sh"
}

ask() {  # ask <prompt> [default] -> echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [ -n "$default" ]; then
    printf '%s %s[%s]%s ' "$prompt" "$DIM" "$default" "$R" > /dev/tty
  else
    printf '%s ' "$prompt" > /dev/tty
  fi
  IFS= read -r reply < /dev/tty || reply=""
  printf '%s' "${reply:-$default}"
}

ask_required() {  # keeps asking until something is typed
  local answer
  while :; do
    answer="$(ask "$@")"
    [ -n "$answer" ] && { printf '%s' "$answer"; return 0; }
    warn "That one is needed."
  done
}

ask_secret() {  # no echo
  local prompt="$1" reply
  printf '%s ' "$prompt" > /dev/tty
  stty -echo < /dev/tty
  IFS= read -r reply < /dev/tty || reply=""
  stty echo < /dev/tty
  printf '\n' > /dev/tty
  printf '%s' "$reply"
}

ask_yn() {  # ask_yn <prompt> <y|n default> -> returns 0 for yes
  local prompt="$1" default="${2:-y}" reply hint
  [ "$default" = "y" ] && hint="Y/n" || hint="y/N"
  while :; do
    printf '%s %s[%s]%s ' "$prompt" "$DIM" "$hint" "$R" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
    reply="${reply:-$default}"
    case "$reply" in
      [Yy]*) return 0 ;;
      [Nn]*) return 1 ;;
      *) warn "Please answer y or n." ;;
    esac
  done
}

ask_choice() {  # ask_choice <default-index> <label…> -> echoes the chosen index
  local default="$1"; shift
  local labels=("$@") i reply
  for i in "${!labels[@]}"; do
    printf '  %s%s)%s %s\n' "$B" "$((i + 1))" "$R" "${labels[$i]}" > /dev/tty
  done
  while :; do
    printf 'Choose %s[%s]%s ' "$DIM" "$default" "$R" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
    reply="${reply:-$default}"
    case "$reply" in
      ''|*[!0-9]*) warn "A number, please." ;;
      *) if [ "$reply" -ge 1 ] && [ "$reply" -le "${#labels[@]}" ]; then
           printf '%s' "$reply"; return 0
         fi
         warn "Between 1 and ${#labels[@]}." ;;
    esac
  done
}

# --- small helpers ------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# Random secrets. openssl where it exists, /dev/urandom otherwise — the subshell
# disables pipefail because `head -c` closing the pipe kills `tr` with SIGPIPE,
# which under `set -o pipefail` would otherwise abort the whole script.
rand() {
  local n="${1:-48}"
  if have openssl; then
    openssl rand -base64 $((n * 2)) | tr -dc 'A-Za-z0-9' | cut -c1-"$n"
  else
    ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$n" )
  fi
}

# TOML string escaping. Passwords are arbitrary text and app-specific passwords
# in particular carry spaces and dashes; a stray " or \ would otherwise produce
# a file that does not parse — after the user has already typed their
# credentials into it.
toml_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

fetch() {  # fetch <url> <dest>
  if have curl; then curl -fsSL "$1" -o "$2"
  elif have wget; then wget -qO "$2" "$1"
  else return 1; fi
}

fetch_stdout() {
  if have curl; then curl -fsSL "$1"
  elif have wget; then wget -qO- "$1"
  else return 1; fi
}

compose() {
  # -f and --env-file are absolute, but the project directory — which is what
  # `./meercal.toml` in the compose file resolves against — follows the compose
  # file, so these work from wherever the user happens to be standing.
  if [ -f "$ENV_FILE" ]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

configured() { [ -f "$COMPOSE_FILE" ] && [ -f "$ENV_FILE" ] && [ -f "$CONFIG_FILE" ]; }

require_configured() {
  configured || die "meercal is not set up on this machine yet. Run: bash $0 setup"
}

env_get() {  # read one KEY=value back out of .env
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1
}

pg_user() { local v; v="$(env_get POSTGRES_USER)"; printf '%s' "${v:-meercal}"; }
pg_db()   { local v; v="$(env_get POSTGRES_DB)";   printf '%s' "${v:-meercal}"; }

web_url() {
  local bind port
  bind="$(env_get MEERCAL_BIND)"; port="$(env_get MEERCAL_PORT)"
  # 0.0.0.0 is what it *listens* on, never an address to visit.
  [ -z "$bind" ] || [ "$bind" = "0.0.0.0" ] && bind="127.0.0.1"
  printf 'http://%s:%s' "$bind" "${port:-8010}"
}

check_docker() {
  have docker || die "Docker is not installed. See https://docs.docker.com/get-docker/"
  docker compose version >/dev/null 2>&1 \
    || die "Docker Compose v2 is missing (the 'docker compose' subcommand, not 'docker-compose')."
  docker info >/dev/null 2>&1 \
    || die "Docker is installed but not running. Start Docker Desktop, or: sudo systemctl start docker"
}

# --- asking about calendars ---------------------------------------------------
#
# One function per kind, each *appending* a `[[agent.account]]` block to the
# file named by $1. Appending to a file rather than writing to stdout because
# everything else these functions do is talk to the reader — and the first
# version of this captured the explanatory paragraphs into the config, where
# tomllib found itself parsing prose.
#
# What the kinds have in common is small; what differs is the part that gets
# people stuck, so each says its own thing about it.

ask_icloud_account() {  # <file>
  local target="$1" label username password
  say ""
  info "Apple needs an ${B}app-specific password${R} for anything that is not Apple's own"
  info "software. Your normal iCloud password will not work, and neither will a"
  info "2FA code. Make one at ${BLU}https://account.apple.com${R} → Sign-In and Security"
  info "→ App-Specific Passwords. It looks like ${DIM}abcd-efgh-ijkl-mnop${R}."
  say ""
  label="$(ask "A name for this account" "iCloud")"
  username="$(ask_required "Your iCloud address:")"
  password="$(ask_secret "App-specific password:")"
  [ -n "$password" ] || die "No password given; nothing would sync."

  cat >> "$target" <<EOF

[[agent.account]]
label = $(toml_str "$label")
kind = "icloud"
username = $(toml_str "$username")
password = $(toml_str "$password")
EOF
}

ask_caldav_account() {  # <file>
  local target="$1" label url username password only
  say ""
  info "Anything that speaks CalDAV: Nextcloud, Fastmail, Radicale, SOGo, mailbox.org."
  info "The URL is the DAV root, not one calendar — meercal discovers the calendars"
  info "from it. For Nextcloud that is ${DIM}https://cloud.example.com/remote.php/dav${R};"
  info "for Fastmail, ${DIM}https://caldav.fastmail.com/dav/${R}."
  say ""
  label="$(ask "A name for this account" "CalDAV")"
  url="$(ask_required "DAV URL:")"
  username="$(ask_required "Username:")"
  password="$(ask_secret "Password:")"
  only="$(ask "Only sync calendars whose name matches (regex, blank for all):" "")"

  cat >> "$target" <<EOF

[[agent.account]]
label = $(toml_str "$label")
kind = "caldav"
url = $(toml_str "$url")
username = $(toml_str "$username")
password = $(toml_str "$password")
EOF
  [ -n "$only" ] && printf 'only = %s\n' "$(toml_str "$only")" >> "$target"
  return 0
}

ask_ics_account() {  # <file>
  local target="$1" label url
  say ""
  info "A published .ics address: school holidays, a colleague's shared calendar,"
  info "or Google's ${B}secret address in iCal format${R} (Google Calendar → Settings →"
  info "the calendar → Integrate calendar). Read-only, and needs no credentials."
  say ""
  label="$(ask "A name for this feed" "Feed")"
  url="$(ask_required "URL of the .ics file:")"

  cat >> "$target" <<EOF

[[agent.account]]
label = $(toml_str "$label")
kind = "ics"
url = $(toml_str "$url")
EOF
}

ask_google_account() {  # <file>
  local target="$1" label username client_id client_secret refresh_token
  say ""
  warn "Google is the awkward one."
  info "Basic auth to Google's CalDAV endpoint has been off for years, so an app"
  info "password will not open it the way it opens Gmail. Two ways in:"
  say ""
  info "  ${B}The easy one${R} — Calendar → Settings → your calendar → Integrate"
  info "  calendar → ${B}Secret address in iCal format${R}, added here as a feed."
  info "  Read-only, no credentials, works today. Answer ${B}n${R} below for that."
  say ""
  info "  ${B}The full one${R} — an OAuth client of your own (Google Cloud Console →"
  info "  Credentials → OAuth client ID → Desktop app), then a refresh token from"
  info "  ${DIM}python -m agent.google_auth${R}. Read and write."
  say ""
  if ! ask_yn "Do you have an OAuth client ID, secret and refresh token to hand?" n; then
    ask_ics_account "$target"
    return
  fi
  label="$(ask "A name for this account" "Google")"
  username="$(ask_required "Your Google address:")"
  client_id="$(ask_required "Client ID:")"
  client_secret="$(ask_secret "Client secret:")"
  refresh_token="$(ask_secret "Refresh token:")"

  cat >> "$target" <<EOF

[[agent.account]]
label = $(toml_str "$label")
kind = "google"
username = $(toml_str "$username")
client_id = $(toml_str "$client_id")
client_secret = $(toml_str "$client_secret")
refresh_token = $(toml_str "$refresh_token")
EOF
}

ask_accounts() {  # ask_accounts <file>
  local target="$1" choice more=1
  while [ "$more" = 1 ]; do
    head1 "Where are your calendars?"
    choice="$(ask_choice 1 \
      "Apple iCloud — the family calendar, with an app-specific password" \
      "CalDAV — Nextcloud, Fastmail, Radicale, SOGo, anything standard" \
      "A published .ics feed — read-only, no credentials" \
      "Google Calendar" \
      "None for now — I will add them later")"
    case "$choice" in
      1) ask_icloud_account "$target" ;;
      2) ask_caldav_account "$target" ;;
      3) ask_ics_account "$target" ;;
      4) ask_google_account "$target" ;;
      5) return 0 ;;
    esac
    ask_yn "Add another account?" n || more=0
  done
}

# --- writing the files --------------------------------------------------------

write_env() {  # write_env <port> <bind> <db_port>
  umask 077
  cat > "$ENV_FILE" <<EOF
# Written by meercal.sh. Container topology and credentials only — everything
# about the calendar itself lives in meercal.toml beside this file.

# The release this install is pinned to. \`meercal.sh update\` moves it.
MEERCAL_VERSION=$(latest_version)

# Who the containers run as. meercal.toml is mode 0600 and holds calendar
# passwords, and a bind mount carries the host's ownership straight through —
# so the containers run as the user that owns the file rather than as root.
MEERCAL_UID=$(id -u)
MEERCAL_GID=$(id -g)

# Where the UI is published. Keep the 127.0.0.1 bind unless you have put TLS
# and a password in front of it — see server.password in meercal.toml.
MEERCAL_BIND=$2
MEERCAL_PORT=$1

# The bundled Postgres. Published on loopback so that backups, psql and an
# agent run from a checkout can reach it. 5433, not 5432: meerail's stack has
# that one, and the two are expected to live on the same machine.
MEERCAL_DB_PORT=$3
POSTGRES_USER=meercal
POSTGRES_PASSWORD=$(rand 32)
POSTGRES_DB=meercal
EOF
  chmod 600 "$ENV_FILE"
}

write_config() {  # write_config <accounts-block-file> <password> <timezone> <week_start> <interval>
  local accounts="$1" password="$2" timezone="$3" week_start="$4" interval="$5"
  umask 077
  {
    cat <<EOF
# meercal — written by meercal.sh on $(date +%Y-%m-%d).
#
# Every setting here can be overridden by an environment variable of the same
# name in upper case; the environment wins over this file. The full reference,
# with everything this file leaves at its default, is in meercal.example.toml:
#   https://github.com/$REPO/blob/main/meercal.example.toml
#
# This file holds calendar passwords in plaintext. It is mode 0600, and the
# agent refuses to run if that changes.

[database]
# db:5432 is handed to the containers by compose; this line is for an agent or
# a psql run from the host, against the port .env publishes.
url = "postgresql+psycopg://meercal:$(env_get POSTGRES_PASSWORD)@127.0.0.1:$(env_get MEERCAL_DB_PORT)/meercal"

[server]
# Signs session cookies and encrypts anything this server stores for you.
secret_key = $(toml_str "$(rand 48)")

# Password gating the UI. Empty means open, which is right for a localhost
# install. Setting it also refuses plain HTTP to anything but loopback.
password = $(toml_str "$password")

# How the calendar is drawn. "system" follows the host's zone.
timezone = $(toml_str "$timezone")
week_start = $week_start

# Places you keep typing, offered as chips under the event panel's Where field.
# The key is the label; the value is what goes into the field.
[server.places]
# "Office" = "Ritterstr. 12, 10969 Berlin"
# "Meet" = "https://meet.example.com/abc-defg"

[agent]
# Seconds between sync passes. CalDAV servers hand out sync tokens, so a pass
# over a calendar with nothing new costs one request.
interval = $interval
EOF
    if [ -s "$accounts" ]; then
      cat "$accounts"
    else
      cat <<'EOF'

# No accounts yet. Add one per calendar account and run `meercal.sh restart`:
#
# [[agent.account]]
# label = "Family"
# kind = "icloud"                       # icloud | caldav | ics | google
# username = "you@icloud.com"
# password = "abcd-efgh-ijkl-mnop"      # app-specific, from account.apple.com
EOF
    fi
  } > "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
}

install_compose_file() {
  # A checkout beside the script wins, so that testing an unreleased change does
  # not need it published first. Otherwise take the one from the release.
  if [ -f "$SELF_DIR/docker-compose.hub.yml" ]; then
    cp "$SELF_DIR/docker-compose.hub.yml" "$COMPOSE_FILE"
    info "Using docker-compose.hub.yml from $SELF_DIR"
  else
    fetch "$RAW_BASE/docker-compose.hub.yml" "$COMPOSE_FILE" \
      || die "Could not download the compose file from $RAW_BASE/docker-compose.hub.yml"
  fi
}

latest_version() {
  # The release itself: CI tags the images with exactly this file's contents.
  local v
  v="$(fetch_stdout "$RAW_BASE/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
  case "$v" in
    ''|*[!0-9.a-zA-Z_-]*) printf 'latest' ;;
    *) printf '%s' "$v" ;;
  esac
}

port_free() {  # port_free <port> — best effort; a busy port is worth catching early
  if have ss; then ! ss -ltn 2>/dev/null | grep -q ":$1 "
  elif have lsof; then ! lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else return 0; fi
}

pick_port() {  # pick_port <first-choice> -> echoes a free one
  local port="$1"
  while ! port_free "$port"; do port=$((port + 1)); done
  printf '%s' "$port"
}

# --- setup --------------------------------------------------------------------

cmd_setup() {
  need_tty
  check_docker

  head1 "meercal"
  say "A calendar for people who have too many calendars."
  say "This writes its configuration to ${B}$MEERCAL_HOME${R}, pulls the images, and starts them."
  rule

  if configured; then
    warn "There is already an install in $MEERCAL_HOME."
    say "  ${B}$0 start${R} runs it · ${B}$0 config${R} edits it · ${B}$0 update${R} upgrades it"
    ask_yn "Reconfigure from scratch? (your calendars in the database are kept)" n || exit 0
  fi

  mkdir -p "$MEERCAL_HOME"
  chmod 700 "$MEERCAL_HOME"

  head1 "Where to publish it"
  local port bind db_port
  port="$(pick_port "${MEERCAL_PORT:-8010}")"
  [ "$port" = "8010" ] || info "8010 is busy, so $port it is."
  port="$(ask "Port for the web UI" "$port")"
  db_port="$(pick_port "${MEERCAL_DB_PORT:-5433}")"

  bind="127.0.0.1"
  say ""
  info "By default only this machine can reach it. Answering yes below publishes"
  info "it on every interface — do that only behind TLS, and set a password when"
  info "asked in a moment."
  ask_yn "Make it reachable from other machines?" n && bind="0.0.0.0"

  local password=""
  head1 "A password?"
  if [ "$bind" = "0.0.0.0" ]; then
    warn "Published beyond this machine, so a password is not optional."
    password="$(ask_secret "Password for the UI:")"
    [ -n "$password" ] || die "Refusing to publish an unprotected calendar on every interface."
  else
    info "A localhost install needs none: your own login is already the gate."
    if ask_yn "Set one anyway?" n; then password="$(ask_secret "Password for the UI:")"; fi
  fi

  head1 "How it should read"
  local timezone week_start interval
  timezone="$(ask "Timezone (\"system\" follows this machine)" "system")"
  week_start=1
  ask_yn "Does your week start on Monday?" y || week_start=7
  interval="$(ask "Seconds between sync passes" "300")"

  # Global, and guarded in the trap: the trap body is evaluated at exit, in a
  # scope where a `local` of this function no longer exists — and under `set -u`
  # that turned a finished install into "unbound variable" and exit 1.
  accounts_file="$(mktemp)"
  trap 'rm -f "${accounts_file:-}"' EXIT
  ask_accounts "$accounts_file"

  head1 "Writing it out"
  write_env "$port" "$bind" "$db_port"
  write_config "$accounts_file" "$password" "$timezone" "$week_start" "$interval"
  install_compose_file
  ok "$CONFIG_FILE"
  ok "$ENV_FILE"
  ok "$COMPOSE_FILE"

  head1 "Pulling the images"
  # Tolerant on purpose: an install from a local build (or a machine that is
  # offline right now) has the images already, and `up` below fails loudly and
  # specifically if one is genuinely missing. A pull that cannot reach the Hub
  # is not a reason to abandon a configuration the user has just typed.
  compose pull || warn "Could not pull from Docker Hub — using whatever is already on this machine."
  head1 "Starting"
  compose up -d
  if ! wait_for_health; then
    head1 "Started, but the server is not answering"
    info "The containers are up and your configuration is written. Look at:"
    info "  ${B}$0 logs server${R}"
    info "  ${B}$0 logs agent${R}"
    info "and once it is fixed, ${B}$0 restart${R}."
    return 1
  fi

  head1 "Ready"
  ok "meercal is at ${B}$(web_url)${R}"
  if [ ! -s "$accounts_file" ]; then
    say ""
    info "No accounts configured, so the calendar is empty. Either:"
    info "  ${B}$0 config${R}  and add an [[agent.account]] block, then ${B}$0 restart${R}"
    info "  ${B}$0 demo${R}    to fill it with a week worth looking at first"
  else
    say ""
    info "The first sync is running now — ${B}$0 logs agent${R} watches it work through"
    info "your calendars. ${B}$0 test${R} checks every account and changes nothing."
  fi
  say ""
  info "${B}$0 help${R} lists everything else."
}

wait_for_health() {
  local url tries=0
  url="$(web_url)/healthz"
  printf '  waiting for the server' > /dev/tty 2>/dev/null || true
  while [ "$tries" -lt 60 ]; do
    if have curl && curl -fsS "$url" >/dev/null 2>&1; then say ""; ok "Server up."; return 0; fi
    if ! have curl && [ "$tries" -gt 5 ]; then say ""; return 0; fi
    printf '.' > /dev/tty 2>/dev/null || true
    sleep 1
    tries=$((tries + 1))
  done
  say ""
  return 1
}

# --- everyday commands --------------------------------------------------------

cmd_start()   { require_configured; compose up -d; ok "Running — $(web_url)"; }
cmd_stop()    { require_configured; compose stop; ok "Stopped. \`start\` brings it back with everything intact."; }
cmd_restart() { require_configured; compose up -d --force-recreate; ok "Restarted — $(web_url)"; }
cmd_logs()    { require_configured; compose logs -f --tail 200 ${1:+"$1"}; }
cmd_psql()    { require_configured; compose exec db psql -U "$(pg_user)" -d "$(pg_db)"; }

cmd_status() {
  require_configured
  head1 "meercal"
  info "home:    $MEERCAL_HOME"
  info "version: $(env_get MEERCAL_VERSION)"
  info "url:     $(web_url)"
  rule
  compose ps
  # What the app itself thinks, which is the question behind "is it working" —
  # a container can be up and an account still not syncing.
  if have curl; then
    local status
    status="$(curl -fsS "$(web_url)/api/sync/status" 2>/dev/null || true)"
    if [ -n "$status" ]; then
      rule
      printf '%s' "$status" | tr ',' '\n' | grep -E '"label"|"stale"|"error"|"queued"' \
        | sed 's/^[{[]*//' | sed 's/^/  /' || true
    fi
  fi
}

cmd_test() {
  require_configured
  head1 "Checking every configured account"
  # `run --rm`, not `exec`: this has to work when the agent is stopped, which
  # is exactly when somebody reaches for it.
  compose run --rm agent python -m agent.main --test
}

cmd_sync() {
  require_configured
  head1 "One sync pass"
  compose run --rm agent python -m agent.main --once
  ok "Done."
}

cmd_demo() {
  require_configured
  compose run --rm server python tools/seed_demo.py --reset
  ok "Demo calendars added — $(web_url)"
  info "They are a normal local account called Demo; delete it in the app when done."
}

# --- backup and restore -------------------------------------------------------

backup_filename() { printf 'meercal-%s.dump' "$(date +%Y%m%d-%H%M%S)"; }

cmd_backup() {
  require_configured
  local dest="${1:-$MEERCAL_HOME/backups/$(backup_filename)}"
  mkdir -p "$(dirname "$dest")"
  head1 "Backup"
  info "→ $dest"
  # -Fc, the custom format: compressed, and restorable into a differently-named
  # database. `exec -T` because there is no terminal on the other end of a pipe.
  compose exec -T db pg_dump -U "$(pg_user)" -d "$(pg_db)" -Fc > "$dest"
  # It is every event you have; keep it as private as the config beside it.
  chmod 600 "$dest"
  ok "$(du -h "$dest" | cut -f1) written."
  info "It holds your events; meercal.toml holds the credentials. Keep both."
}

cmd_restore() {
  require_configured
  local src="${1:-}"
  [ -n "$src" ] || die "usage: $0 restore <file.dump>"
  [ -f "$src" ] || die "No such file: $src"
  need_tty
  warn "This replaces everything currently in the database."
  ask_yn "Restore $src?" n || exit 0
  # --clean --if-exists so a restore over a populated database works; the app
  # is stopped first so nothing writes underneath it.
  compose stop server agent >/dev/null 2>&1 || true
  compose exec -T db pg_restore -U "$(pg_user)" -d "$(pg_db)" --clean --if-exists < "$src"
  compose up -d
  ok "Restored — $(web_url)"
}

# --- lifecycle ----------------------------------------------------------------

cmd_update() {
  require_configured
  local current latest
  current="$(env_get MEERCAL_VERSION)"
  latest="$(latest_version)"
  head1 "Update"
  info "installed: ${current:-unknown}"
  info "latest:    $latest"
  if [ "$current" = "$latest" ]; then
    ok "Already on the newest release."
    need_tty
    ask_yn "Re-pull the images anyway?" n || return 0
  fi

  # The compose file ships with the release, so it moves with it — a new
  # service or a renamed variable would otherwise be missed on upgrade.
  install_compose_file
  if [ -n "$latest" ] && [ "$latest" != "latest" ]; then
    # sed -i differs between GNU and BSD; write a new file and move it instead.
    sed "s/^MEERCAL_VERSION=.*/MEERCAL_VERSION=$latest/" "$ENV_FILE" > "$ENV_FILE.new"
    mv "$ENV_FILE.new" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  fi
  compose pull
  compose up -d
  ok "Now on $latest — $(web_url)"
  info "The database migrates itself on first boot; nothing else to do."
}

cmd_config() {
  require_configured
  local editor="${VISUAL:-${EDITOR:-}}"
  if [ -z "$editor" ]; then
    for candidate in nano vim vi; do have "$candidate" && { editor="$candidate"; break; }; done
  fi
  [ -n "$editor" ] || die "No editor found. Edit $CONFIG_FILE yourself."
  "$editor" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  ask_yn "Restart so the changes take effect?" y && cmd_restart
}

cmd_uninstall() {
  require_configured
  need_tty
  head1 "Uninstall"
  warn "This stops the containers and removes them."
  local drop=1
  ask_yn "Also delete the database volume — every event meercal holds?" n || drop=0
  if [ "$drop" = 1 ]; then
    compose down -v
    ok "Containers and data removed."
  else
    compose down
    ok "Containers removed; the database volume is still there."
    info "A later \`$0 setup\` will find it and pick up where this left off."
  fi
  if ask_yn "Delete $MEERCAL_HOME (configuration and any backups in it)?" n; then
    rm -rf "$MEERCAL_HOME"
    ok "Gone."
  else
    info "Kept: $MEERCAL_HOME"
  fi
}

cmd_version() {
  say "meercal.sh"
  if configured; then
    info "installed: $(env_get MEERCAL_VERSION)"
    info "latest:    $(latest_version)"
  else
    info "not set up on this machine"
    info "latest:    $(latest_version)"
  fi
}

cmd_help() {
  cat <<EOF
${B}meercal${R} — a calendar for people who have too many calendars

  ${B}bash $0${R}                 set it up (the default), or reconfigure

  ${B}$0 start${R}       start the containers
  ${B}$0 stop${R}        stop them, keeping everything
  ${B}$0 restart${R}     restart them, picking up config changes
  ${B}$0 status${R}      what is running, and whether the accounts are syncing
  ${B}$0 logs${R} [svc]  follow the logs — svc is server, agent or db
  ${B}$0 test${R}        check every configured calendar account, change nothing
  ${B}$0 sync${R}        run one sync pass now and print what it did
  ${B}$0 config${R}      edit meercal.toml, then restart
  ${B}$0 demo${R}        fill it with demo calendars worth looking at
  ${B}$0 update${R}      pull the newest release and restart
  ${B}$0 backup${R} [f]  dump the database (default: $MEERCAL_HOME/backups)
  ${B}$0 restore${R} <f> restore such a dump over the current database
  ${B}$0 psql${R}        a psql shell on the database
  ${B}$0 uninstall${R}   remove the containers, and optionally the data
  ${B}$0 version${R}     what is installed, and what is out

Files, all under ${B}$MEERCAL_HOME${R}:

  meercal.toml        your configuration — calendars, password, places (0600)
  .env                ports, database credentials, the pinned version
  docker-compose.yml  the release's own compose file
  backups/            whatever ${B}backup${R} has written

Your events live in the Docker volume ${B}meercal-db${R}, not in that directory.

Environment:
  MEERCAL_HOME        where all of the above goes (default ~/.meercal)
  MEERCAL_REPO        the repository to update from (default $REPO)
EOF
}

# --- dispatch -----------------------------------------------------------------

main() {
  local cmd="${1:-setup}"
  [ $# -gt 0 ] && shift || true
  case "$cmd" in
    setup|install|"")  cmd_setup "$@" ;;
    start|up)          cmd_start "$@" ;;
    stop|down)         cmd_stop "$@" ;;
    restart)           cmd_restart "$@" ;;
    status|ps)         cmd_status "$@" ;;
    logs|log)          cmd_logs "$@" ;;
    test|check)        cmd_test "$@" ;;
    sync)              cmd_sync "$@" ;;
    demo|seed)         cmd_demo "$@" ;;
    config|edit)       cmd_config "$@" ;;
    update|upgrade)    cmd_update "$@" ;;
    backup)            cmd_backup "$@" ;;
    restore)           cmd_restore "$@" ;;
    psql|db)           cmd_psql "$@" ;;
    uninstall|remove)  cmd_uninstall "$@" ;;
    version|--version) cmd_version "$@" ;;
    help|--help|-h)    cmd_help "$@" ;;
    *) die "Unknown command: $cmd (try: $0 help)" ;;
  esac
}

main "$@"
