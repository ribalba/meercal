"""The attendee field's two questions, and the failures that used to be silent.

The endpoints read meerail's database, not meercal's, so the fixtures here
build meerail's two tables (`contacts`, `contact_pairs`) in the throwaway test
database and point the setting at it. That is enough: meercal only ever runs
two SELECTs against them, and this is both of them.

    make test-db test
"""

from __future__ import annotations

import os

import pytest

from app.routers import contacts as mod


# --- the parts that need no database ---------------------------------------

def test_a_failure_never_carries_the_password_out():
    """The reason reaches a browser, and the DSN it names has a password in it."""
    exc = Exception(
        'connection to server at "//meerail:hunter2@db" failed: Connection refused'
    )
    reason = mod._reason(exc)
    assert "hunter2" not in reason
    assert "Connection refused" in reason


def test_a_reason_is_one_line_and_bounded():
    reason = mod._reason(Exception("first line\nsecond line\n" + "x" * 500))
    assert reason == "first line"
    assert len(mod._reason(Exception("y" * 500))) <= 200


def test_a_reason_falls_back_to_the_class_name():
    """An exception carrying no text still has to say something."""
    assert mod._reason(ValueError("")) == "ValueError"


def test_wildcards_typed_into_the_box_are_characters_not_operators():
    """`_` matched every address in the book, which is not what typing it means."""
    assert mod._like("a_b%c") == "%abc%"
    assert mod._like("arne") == "%arne%"


def test_nothing_is_configured_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(mod.settings, "meerail_database_url", "")
    mod._engine.cache_clear()
    assert mod.contacts(q="arne") == {"configured": False, "people": []}
    assert mod.related(address=["a@b.c"]) == {"configured": False, "people": []}
    mod._engine.cache_clear()


# --- the parts that do ------------------------------------------------------
#
# Over HTTP rather than by calling the endpoint functions: their defaults are
# FastAPI `Query` objects, which only become numbers when a request resolves
# them. Calling one directly hands psycopg a Query to bind and proves nothing
# about the thing the browser talks to.

pg = pytest.mark.skipif(
    not os.environ.get("MEERCAL_TEST_DB"), reason="MEERCAL_TEST_DB is not set"
)

# Two people who go everywhere together, one who came along once, and one the
# user writes to constantly on her own -- who is the case the damping exists
# for, and who must therefore not be suggested beside everybody.
_SEED_SQL = """
DROP TABLE IF EXISTS contact_pairs;
DROP TABLE IF EXISTS contacts;
CREATE TABLE contacts (
    address varchar(320) PRIMARY KEY, name varchar(512) NOT NULL,
    count integer NOT NULL, last_seen timestamp);
CREATE TABLE contact_pairs (
    address_a varchar(320) NOT NULL, address_b varchar(320) NOT NULL,
    count integer NOT NULL, weight integer NOT NULL, last_seen timestamp);
INSERT INTO contacts (address, name, count, last_seen) VALUES
    ('ada@example.org',   'Ada Lovelace',   40, '2026-08-01'),
    ('bob@example.org',   'Bob Vance',      40, '2026-08-01'),
    ('once@example.org',  'Passing Acquaintance', 5, '2026-01-01'),
    ('busy@example.org',  'Everybody''s Correspondent', 10000, '2026-08-20');
INSERT INTO contact_pairs (address_a, address_b, count, weight, last_seen) VALUES
    ('ada@example.org',  'bob@example.org',  20, 40, '2026-08-01'),
    ('bob@example.org',  'ada@example.org',  20, 40, '2026-08-01'),
    ('ada@example.org',  'busy@example.org', 30, 60, '2026-08-20'),
    ('busy@example.org', 'ada@example.org',  30, 60, '2026-08-20'),
    ('ada@example.org',  'once@example.org',  1,  1, '2026-01-01'),
    ('once@example.org', 'ada@example.org',  1,  1, '2026-01-01');
"""


@pytest.fixture(scope="module")
def client():
    fastapi_testclient = pytest.importorskip("fastapi.testclient")

    from app.main import app

    with fastapi_testclient.TestClient(app) as c:   # lifespan runs init_db
        yield c


@pytest.fixture
def meerail(monkeypatch):
    """meerail's two tables, in the test database, behind the setting."""
    from sqlalchemy import create_engine, text

    url = os.environ["MEERCAL_TEST_DB"]
    engine = create_engine(url)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _SEED_SQL.split(";"))):
            conn.execute(text(statement))
    monkeypatch.setattr(mod.settings, "meerail_database_url", url)
    mod._engine.cache_clear()
    yield
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS contact_pairs"))
        conn.execute(text("DROP TABLE IF EXISTS contacts"))
    engine.dispose()
    mod._engine.cache_clear()


def _emails(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [p["email"] for p in response.json()["people"]]


def _related(client, *addresses, **params):
    query = [("address", a) for a in addresses] + list(params.items())
    return client.get("/api/contacts/related", params=query)


@pg
def test_the_typeahead_finds_a_person_by_address_or_by_name(client, meerail):
    assert _emails(client.get("/api/contacts", params={"q": "ada"})) == ["ada@example.org"]
    assert _emails(client.get("/api/contacts", params={"q": "Vance"})) == ["bob@example.org"]


@pg
def test_one_letter_is_not_a_search(client, meerail):
    """Held back on purpose: a single letter matches the whole address book."""
    assert _emails(client.get("/api/contacts", params={"q": "a"})) == []


@pg
def test_the_people_you_write_to_most_come_first(client, meerail):
    found = _emails(client.get("/api/contacts", params={"q": "example.org"}))
    assert found[0] == "busy@example.org"


@pg
def test_who_normally_comes_with_this_person(client, meerail):
    """The feature: one name on the invitation, and the rest of the room offered."""
    assert _emails(_related(client, "ada@example.org"))[0] == "bob@example.org"


@pg
def test_somebody_you_write_to_constantly_is_not_suggested_beside_everybody(client, meerail):
    """busy@ has the higher raw weight (60 against 40) and still ranks below bob@:
    that is the sqrt damping, and without it the answer is the same name every
    time regardless of who the meeting is with."""
    order = _emails(_related(client, "ada@example.org"))
    assert order.index("bob@example.org") < order.index("busy@example.org")


@pg
def test_a_single_shared_mail_is_a_coincidence_not_a_pattern(client, meerail):
    """weight 1 -- one message that merely happened to carry both names."""
    assert "once@example.org" not in _emails(_related(client, "ada@example.org"))


@pg
def test_nobody_already_invited_is_offered_again(client, meerail):
    found = _emails(_related(client, "ada@example.org", "bob@example.org"))
    assert set(found).isdisjoint({"ada@example.org", "bob@example.org"})


@pg
def test_seeds_are_cleaned_up_before_they_are_asked_about(client, meerail):
    """The field is read straight off the screen: casing, surrounding space, a
    half-typed name with no @ in it yet, and the same person named twice."""
    found = _emails(_related(
        client, "  ADA@Example.org ", "", "half-typed", "ada@example.org"))
    assert found[0] == "bob@example.org"


@pg
def test_no_seeds_is_a_question_with_no_answer(client, meerail):
    assert _emails(_related(client)) == []
    assert _emails(_related(client, "not-an-address")) == []


@pg
def test_the_offer_is_a_short_list(client, meerail):
    assert _emails(_related(client, "ada@example.org", limit=1)) == ["bob@example.org"]


@pg
def test_a_meerail_that_is_configured_but_broken_says_so(client, monkeypatch):
    """The bug this endpoint had: unreachable and empty look identical, so the
    field silently offered nobody while /api/state reported meerail as on."""
    monkeypatch.setattr(
        mod.settings, "meerail_database_url",
        "postgresql+psycopg://nobody:secret@127.0.0.1:1/nothing",
    )
    mod._engine.cache_clear()
    try:
        responses = [
            client.get("/api/contacts", params={"q": "ada"}),
            _related(client, "a@b.c"),
        ]
        for response in responses:
            payload = response.json()
            assert payload["configured"] is True
            assert payload["people"] == []
            assert payload["error"], "an unreachable meerail has to say so"
            assert "secret" not in payload["error"]
    finally:
        mod._engine.cache_clear()
