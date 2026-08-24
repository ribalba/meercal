#!/usr/bin/env bash
# A throwaway CalDAV server for the integration test.
#
#   tools/caldav_test_server.sh start     # http://127.0.0.1:5232, didi/secret
#   tools/caldav_test_server.sh stop
#
# Radicale, because it is a real CalDAV implementation in one small container:
# the agent's discovery, sync-collection, multiget and PUT paths all run against
# it exactly as they would against iCloud. That is the half of this program that
# cannot be unit-tested — a mock of a protocol only proves the mock.
set -euo pipefail

NAME=meercal-radicale
PORT=${CALDAV_PORT:-5232}
DIR=${CALDAV_STATE:-/tmp/meercal-radicale}

start() {
  mkdir -p "$DIR/config" "$DIR/data"
  # Storage under /data: the image runs as uid 2999 and cannot write the
  # default /var/lib/radicale, which fails at startup with a permission error
  # that says nothing about the mount.
  cat > "$DIR/config/config" <<CFG
[server]
hosts = 0.0.0.0:5232

[auth]
type = htpasswd
htpasswd_filename = /config/users
htpasswd_encryption = plain

[storage]
filesystem_folder = /data/collections
CFG
  printf 'didi:secret\n' > "$DIR/config/users"
  chmod -R a+rX "$DIR/config"
  chmod 777 "$DIR/data"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # :z relabels for SELinux; without it the mounts are invisible on Fedora.
  docker run -d --name "$NAME" -p "127.0.0.1:$PORT:5232" \
    -v "$DIR/config:/config:ro,z" -v "$DIR/data:/data:z" \
    tomsquest/docker-radicale:latest >/dev/null
  for _ in $(seq 1 30); do
    if curl -fsS -u didi:secret -X PROPFIND "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
      echo "caldav test server on http://127.0.0.1:$PORT (didi/secret)"
      exit 0
    fi
    sleep 1
  done
  echo "radicale did not come up:" >&2
  docker logs "$NAME" 2>&1 | tail -20 >&2
  exit 1
}

case "${1:-start}" in
  start) start ;;
  stop) docker rm -f "$NAME" >/dev/null 2>&1 && echo "stopped" ;;
  *) echo "usage: $0 [start|stop]" >&2; exit 2 ;;
esac
