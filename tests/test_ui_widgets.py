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
