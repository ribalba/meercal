# meercal-agent — the half that holds your calendar passwords.
#
# Unlike meerail's agent, this one has nothing on the host it needs to reach:
# CalDAV servers are out on the internet, so the agent is an ordinary container
# on the compose network. The credentials still never leave your machine — they
# live in meercal.toml, mounted read-only into this container and nowhere else.
FROM python:3.13-slim

ARG MEERCAL_VERSION=0.0.0

LABEL org.opencontainers.image.title="meercal-agent" \
      org.opencontainers.image.description="Calendar sync for meercal: CalDAV, iCloud, Google, .ics" \
      org.opencontainers.image.source="https://github.com/ribalba/meercal" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${MEERCAL_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY core /app/core
COPY agent /app/agent
COPY VERSION /app/VERSION
# The build's own number wins over whatever the tree happened to hold, so an
# image cannot claim a version it was not built from.
RUN [ "$MEERCAL_VERSION" = "0.0.0" ] || printf '%s\n' "$MEERCAL_VERSION" > /app/VERSION

RUN useradd --create-home --uid 10001 meercal && chown -R meercal /app
USER meercal

# No healthcheck: a sync pass can legitimately take minutes on a first run, and
# an agent between passes is idle by design. Whether it is working is a question
# about `accounts.last_sync_at`, which the UI already answers.
CMD ["python", "-m", "agent.main"]
