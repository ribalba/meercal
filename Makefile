# meercal — convenience targets.
#
# The server is Dockerized; the agent runs natively, because it is the half
# that holds your calendar passwords. `make up` runs the app, `make agent` runs
# the connector.

COMPOSE ?= docker compose
VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
VERSION := $(shell cat VERSION)

.PHONY: help up down logs build infra dev venv agent agent-test seed psql fmt test test-db desktop version

help:
	@echo "meercal $(VERSION):"
	@echo "  make up         - build + run the stack (server + postgres)"
	@echo "  make down       - stop it"
	@echo "  make logs       - tail the server"
	@echo "  make infra      - run only postgres (for native server dev)"
	@echo "  make dev        - run the server natively with --reload (needs 'make infra' + venv)"
	@echo "  make venv       - create $(VENV) and install server + agent deps"
	@echo "  make agent      - run meercal-agent natively (reads meercal.toml)"
	@echo "  make agent-test - check every configured calendar account, then exit"
	@echo "  make seed       - fill the database with a demo calendar set"
	@echo "  make psql       - a shell on the database"
	@echo "  make desktop    - run the Electron app against the local server"
	@echo "  make test       - run the test suite"
	@echo "  make test-db    - create the throwaway database the API tests need"

up:
	$(COMPOSE) up --build -d
	@echo "meercal on http://127.0.0.1:$${MEERCAL_PORT:-8010}"

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

seed:
	$(PY) tools/seed_demo.py

desktop:
	cd electron && npm install && npm start

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-meercal} -d $${POSTGRES_DB:-meercal}

# A database of its own, dropped and rebuilt by the tests. Never the one your
# calendars are in — the API tests start by dropping every table they find.
TEST_DB ?= postgresql+psycopg://meercal:meercal@127.0.0.1:$${MEERCAL_DB_PORT:-5433}/meercal_test

test-db:
	$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-meercal} -d postgres \
	  -c "DROP DATABASE IF EXISTS meercal_test" \
	  -c "CREATE DATABASE meercal_test"

test:
	MEERCAL_TEST_DB=$(TEST_DB) $(VENV)/bin/pytest -q

version:
	@echo $(VERSION)
