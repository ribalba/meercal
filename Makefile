# meercal: convenience targets.
#
# `make up` runs the whole stack in Docker -- postgres, server and agent -- the
# same three containers meercal.sh installs. `make agent` runs the connector
# natively instead, which is the faster loop for working on it and the only way
# to reach the host: OAuth and desktop reminders need your session.

COMPOSE ?= docker compose
VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
VERSION := $(shell cat VERSION)

# --- release images ----------------------------------------------------------
#
# The two images `meercal.sh` pulls. VERSION is the single source of the number:
# it tags the images, stamps their OCI labels, and is what an install compares
# itself against to notice an update.
DOCKER_ORG ?= ribalba
# Both architectures the project claims to support: Intel/AMD servers and Apple
# Silicon. Nothing here is compiled per-arch (the Python dependencies all ship
# aarch64 wheels), so the emulated half is not as slow as it sounds.
PLATFORMS  ?= linux/amd64,linux/arm64
# A named builder, because the default `docker` driver cannot do multi-platform
# builds at all. Created on demand by the buildx target below.
BUILDER    ?= meercal

.PHONY: help up up-meerail down logs build infra dev venv agent agent-test remind remind-test \
        remind-next seed psql fmt test test-db \
        desktop hub-up hub-down buildx images push images-push version

help:
	@echo "meercal $(VERSION):"
	@echo "  make up         - build + run the stack (postgres + server + agent)"
	@echo "  make up-meerail - the same, joined to meerail's network (attendee autocomplete)"
	@echo "  make down       - stop it"
	@echo "  make logs       - tail the server ($(COMPOSE) logs -f agent for the other half)"
	@echo "  make infra      - run only postgres (for native server dev)"
	@echo "  make dev        - run the server natively with --reload (needs 'make infra' + venv)"
	@echo "  make venv       - create $(VENV) and install server + agent deps"
	@echo "  make agent      - run meercal-agent natively instead of in its container"
	@echo "  make agent-test - check every configured calendar account, then exit"
	@echo "  make remind-test- one real notification through every reminder channel"
	@echo "  make remind-next- what reminders would fire in the next 24 hours"
	@echo "  make seed       - fill the database with a demo calendar set"
	@echo "  make psql       - a shell on the database"
	@echo "  make desktop    - run the Electron app against the local server"
	@echo "  make test       - run the test suite"
	@echo "  make images     - build the release images for this machine"
	@echo "  make push       - build them for every platform and push to Docker Hub"
	@echo "  make hub-up     - run the published-image stack (what meercal.sh installs)"
	@echo "  make test-db    - create the throwaway database the API tests need"

up:
	MEERCAL_UID=$$(id -u) MEERCAL_GID=$$(id -g) $(COMPOSE) up --build -d
	@echo "meercal on http://127.0.0.1:$${MEERCAL_PORT:-8010}"

# The same stack with the server on meerail's compose network as well, which is
# what the attendee field needs: meerail's Postgres is published on the host's
# loopback and a container cannot reach that. See docker-compose.meerail.yml,
# which also says what [meerail] database_url has to be for this to pay off.
up-meerail:
	MEERCAL_UID=$$(id -u) MEERCAL_GID=$$(id -g) \
	  $(COMPOSE) -f docker-compose.yml -f docker-compose.meerail.yml up --build -d
	@echo "meercal on http://127.0.0.1:$${MEERCAL_PORT:-8010}, reading meerail's address book"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f server

build:
	$(COMPOSE) build

infra:
	$(COMPOSE) up -d db

venv:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt -r agent/requirements.txt
	@echo "ready: $(VENV)"

dev:
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

agent:
	$(PY) -m agent.main

agent-test:
	$(PY) -m agent.main --test

# Reminders normally run inside `make agent`, on a thread of their own. These
# are the two you type by hand: prove the channels work, and see what is about
# to happen before it happens.
remind:
	$(PY) -m agent.remind

remind-test:
	$(PY) -m agent.remind --test

NEXT ?= 24h
remind-next:
	$(PY) -m agent.remind --next $(NEXT)

seed:
	$(PY) tools/seed_demo.py

desktop:
	cd electron && npm install && npm start

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-meercal} -d $${POSTGRES_DB:-meercal}

# A database of its own, dropped and rebuilt by the tests. Never the one your
# calendars are in: the API tests start by dropping every table they find.
TEST_DB ?= postgresql+psycopg://meercal:meercal@127.0.0.1:$${MEERCAL_DB_PORT:-5433}/meercal_test

test-db:
	$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-meercal} -d postgres \
	  -c "DROP DATABASE IF EXISTS meercal_test" \
	  -c "CREATE DATABASE meercal_test"

test:
	MEERCAL_TEST_DB=$(TEST_DB) $(VENV)/bin/pytest -q

# The stack an install actually runs, from the images `make images` just built.
# Pinning the tag to VERSION rather than `latest` is what makes this a test of
# the thing about to be pushed rather than of whatever is on the Hub.
hub-up:
	MEERCAL_VERSION=$(VERSION) MEERCAL_UID=$$(id -u) MEERCAL_GID=$$(id -g) \
	  $(COMPOSE) -f docker-compose.hub.yml up -d
	@echo "meercal on http://127.0.0.1:$${MEERCAL_PORT:-8010}"

hub-down:
	$(COMPOSE) -f docker-compose.hub.yml down

buildx:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 \
	  || docker buildx create --name $(BUILDER) --driver docker-container --bootstrap
	@docker buildx use $(BUILDER)

# Native-architecture build, loaded locally: what you want before pushing
# anything, and what `make hub-up` will find.
images:
	docker build --build-arg MEERCAL_VERSION=$(VERSION) \
	  -t $(DOCKER_ORG)/meercal-server:$(VERSION) -t $(DOCKER_ORG)/meercal-server:latest .
	docker build --build-arg MEERCAL_VERSION=$(VERSION) -f agent/Dockerfile \
	  -t $(DOCKER_ORG)/meercal-agent:$(VERSION) -t $(DOCKER_ORG)/meercal-agent:latest .
	@echo
	@echo "Built $(DOCKER_ORG)/meercal-{server,agent}:$(VERSION) for this machine."

# Publishes. Needs `docker login` first, and push rights on $(DOCKER_ORG).
# Every image gets both tags in one go, so :latest and :$(VERSION) can never
# point at different builds, which is the failure that has people debugging a
# version they are not running.
push: images-push

images-push: buildx
	docker buildx build --platform $(PLATFORMS) --build-arg MEERCAL_VERSION=$(VERSION) \
	  -t $(DOCKER_ORG)/meercal-server:$(VERSION) -t $(DOCKER_ORG)/meercal-server:latest --push .
	docker buildx build --platform $(PLATFORMS) --build-arg MEERCAL_VERSION=$(VERSION) \
	  -f agent/Dockerfile \
	  -t $(DOCKER_ORG)/meercal-agent:$(VERSION) -t $(DOCKER_ORG)/meercal-agent:latest --push .
	@echo
	@echo "Pushed $(DOCKER_ORG)/meercal-{server,agent}:$(VERSION) (+ :latest) for $(PLATFORMS)."
	@echo "Installs see the new version once VERSION is on main; 'meercal.sh update' takes it."

version:
	@echo $(VERSION)
