"""Regression tests for the player-search matcher in ui_widgets.py."""

import ui_widgets as W


def _options(names):
    return [(i, name, name, W._player_search_key(name))
            for i, name in enumerate(names)]


def test_a_misspelled_surname_still_finds_its_player():
    """No substring tier can match "jaims" inside "james" -- one letter is
    simply wrong, not missing -- so this is only findable by similarity."""
    options = _options(["Lebron James", "Lebron Watson"])
    result = W._fuzzy_player_matches(["lebron", "jaims"], options)
    names = [name for *_, name, _ in result]
    assert "Lebron James" in names


def test_one_matching_word_does_not_drag_in_an_unrelated_second_name():
    """"Lebron Jaims" shares a first name with "Lebron Watson", but the
    surname is nowhere close -- the weakest-word rule must reject it, the
    same way tier 3's substring AND-rule would reject a genuine mismatch."""
    options = _options(["Lebron James", "Lebron Watson"])
    result = W._fuzzy_player_matches(["lebron", "jaims"], options)
    names = [name for *_, name, _ in result]
    assert "Lebron Watson" not in names


def test_a_wildly_different_query_gets_no_fuzzy_suggestion():
    """Below the cutoff a suggestion is more likely to mislead than help,
    so an unrelated name must not be offered as a "closest" match."""
    options = _options(["Lebron James"])
    result = W._fuzzy_player_matches(["zzyzx", "qqrrst"], options)
    assert result == []


def test_player_matches_only_falls_back_to_fuzzy_when_nothing_else_hit(
        monkeypatch):
    """A real substring match must win outright, never be out-ranked by a
    fuzzy guess -- the fallback only runs when the strict tiers found
    nothing at all."""
    calls = []
    monkeypatch.setattr(
        W, "_fuzzy_player_matches",
        lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(
        W, "player_options", lambda *a, **k: _options(["Lebron James"]))

    matches = W.player_matches("lebron", sport=type(
        "S", (), {"key": "nba", "db": ":memory:"})(), db_revision=0)
    assert [name for _, name, _ in matches] == ["Lebron James"]
    assert calls == []


def test_player_matches_falls_back_to_fuzzy_on_a_typo(monkeypatch):
    monkeypatch.setattr(
        W, "player_options", lambda *a, **k: _options(["Lebron James"]))

    matches = W.player_matches("lebron jaims", sport=type(
        "S", (), {"key": "nba", "db": ":memory:"})(), db_revision=0)
    assert [name for _, name, _ in matches] == ["Lebron James"]


def test_a_short_query_never_triggers_the_fuzzy_scan(monkeypatch):
    """Two letters are too little signal for a similarity score to mean
    anything, and every name would score high enough to qualify."""
    monkeypatch.setattr(
        W, "player_options", lambda *a, **k: _options(["Lebron James"]))
    monkeypatch.setattr(
        W, "_fuzzy_player_matches",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))

    matches = W.player_matches("zz", sport=type(
        "S", (), {"key": "nba", "db": ":memory:"})(), db_revision=0)
    assert matches == []


# ----------------------------------------------- axis labels on the board

def test_a_builder_template_placeholder_never_reaches_the_board():
    """axis_widget falls back to the BUILDERS key when it has no explicit
    rule for a kind, and fifteen of those keys are templates. The Grid
    Solver drew a literal "X+ CM TALL" heading over a square that was,
    correctly, solving for 195."""
    import sports

    assert W._fill_placeholders(
        "X+ cm tall", [195], sports.AFL.vocab) == "195+ cm tall"
    assert W._fill_placeholders(
        "X cm or shorter", [180], sports.AFL.vocab) == "180 cm or shorter"


def test_the_placeholder_takes_the_number_not_the_first_argument():
    """"Won an award X+ times" is built from (award, times), so filling
    positionally would print the award slug where the count belongs."""
    import sports

    assert W._fill_placeholders(
        "Won an award X+ times", ["all-australian", 3],
        sports.AFL.vocab) == "Won an award 3+ times"


def test_a_crowd_size_is_grouped_so_it_can_be_read_at_a_glance():
    import sports

    assert W._fill_placeholders(
        "Played before a crowd of X+", [50000],
        sports.AFL.vocab) == "Played before a crowd of 50,000+"


def test_no_builder_key_in_any_sport_can_leave_a_placeholder_behind():
    """The guarantee, stated over the real registries rather than a list
    of the keys that happened to be templates when this was written."""
    import re

    import sports

    for sport in (sports.AFL, sports.NBA, sports.MLB, sports.NFL):
        for kind, (_fn, argnames) in sport.C.BUILDERS.items():
            if not re.search(r"\b[XY]\b", kind):
                continue
            # two numbers is the most any template asks for
            filled = W._fill_placeholders(kind, [7, 9], sport.vocab)
            assert not re.search(r"\b[XY]\b", filled), (
                f"{sport.key}: {kind!r} still reads {filled!r}")


# ----------------------------------------------- accented names in search

def test_an_accented_query_finds_the_player_it_names(monkeypatch):
    """"acuna", typed with or without the accent, both have to reach
    "Ronald Acuña" -- 218 MLB player names carry a diacritic."""
    monkeypatch.setattr(
        W, "player_options",
        lambda *a, **k: _options(["Ronald Acuña", "Dakota Bacus"]))
    sport = type("S", (), {"key": "mlb", "db": ":memory:"})()

    for query in ("acuna", "acuña"):
        matches = W.player_matches(query, sport=sport, db_revision=0)
        assert [name for _, name, _ in matches] == ["Ronald Acuña"]


def test_an_accent_stripped_to_a_bare_letter_cannot_admit_a_stranger():
    """Before diacritics were folded into their base letter, the [^a-z0-9]
    regex alone turned "acuña" into two tokens, "acu" and "a" -- and a
    bare single-letter token substring-matches almost any name. That
    admitted "Dakota Bacus" under the AND-substring rule: "acu" is inside
    "bAcUs", and "a" is inside "dAkota"."""
    options = _options(["Ronald Acuña", "Dakota Bacus"])
    q_words = W._player_search_key("acuña").split()
    assert q_words == ["acuna"], "accent should fold, not split the query"

    admitted = [
        name for _, name, _, search_key in options
        if all(any(token in word for word in search_key.split())
              for token in q_words)
    ]
    assert admitted == ["Ronald Acuña"]


def test_diacritics_of_every_shape_normalise_to_their_ascii_letter():
    cases = {
        "José": "jose",
        "Björn": "bjorn",
        "Džems": "dzems",       # caron
        "Dončić": "doncic",    # caron mid-name, NBA-style
    }
    for name, expected in cases.items():
        assert W._player_search_key(name) == expected
