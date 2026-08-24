from app.query import IS_FLAGS, parse_query


def test_bare_words_are_words():
    spec = parse_query("quarterly planning")
    assert spec.words == ["quarterly", "planning"]
    assert not spec.calendars and not spec.flags


def test_filters_are_picked_out_in_any_order():
    spec = parse_query("is:span standup cal:work with:anna in:berlin")
    assert spec.words == ["standup"]
    assert spec.calendars == ["work"]
    assert spec.people == ["anna"]
    assert spec.places == ["berlin"]
    assert spec.flags == ["span"]


def test_a_quoted_phrase_stays_whole():
    assert parse_query('"jour fixe"').words == ["jour fixe"]


def test_an_unbalanced_quote_while_typing_does_not_raise():
    assert parse_query('"jour fixe').words


def test_rx_prefix_turns_on_regex_for_the_query():
    spec = parse_query("rx:standup|jour")
    assert spec.regex is True
    assert spec.words == ["standup|jour"]


def test_an_unknown_is_flag_is_ignored_rather_than_matched():
    assert parse_query("is:unicorn").flags == []
    assert set(parse_query(" ".join(f"is:{f}" for f in IS_FLAGS)).flags) == set(IS_FLAGS)


def test_an_empty_query_is_falsy():
    assert not parse_query("")
    assert parse_query("x")
