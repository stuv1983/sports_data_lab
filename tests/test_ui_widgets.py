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


# --------------------------------------- resolving a typed name to a player

def _matches(names):
    return [(i, name, name) for i, name in enumerate(names)]


def test_a_search_with_one_result_needs_no_second_widget():
    """The dropdown existed to choose between matches. With one match there
    is nothing to choose, so the reader should never see it."""
    assert W.resolve_typed_player(
        "acuna", _matches(["Ronald Acuña"])) == (0, "Ronald Acuña")


def test_a_fully_typed_name_wins_over_the_others_it_is_inside():
    """"Ronald Acuna" is a substring search that rightly also finds
    Luisangel Acuña -- but the text typed *is* one player's whole name, and
    that player is the one meant."""
    resolved = W.resolve_typed_player(
        "Ronald Acuna", _matches(["Ronald Acuña", "Luisangel Acuña"]))
    assert resolved == (0, "Ronald Acuña")


def test_a_partial_name_with_several_matches_still_asks():
    """"acuna" alone names neither of them, so guessing would be wrong."""
    assert W.resolve_typed_player(
        "acuna", _matches(["Ronald Acuña", "Luisangel Acuña"])) is None


def test_a_name_two_players_share_is_never_resolved_silently():
    """Exactly-typed is not the same as unambiguous. Picking the first of
    two Bobby Joneses would quietly answer about the wrong career."""
    assert W.resolve_typed_player(
        "Bobby Jones", _matches(["Bobby Jones", "Bobby Jones"])) is None


def test_no_match_resolves_to_nothing():
    assert W.resolve_typed_player("nobody", []) is None


def test_starting_a_new_round_leaves_the_guess_box_empty(monkeypatch):
    """Every Game Lab mode resets its picker between rounds. They used to do
    it by naming this widget's session keys themselves, so renaming one left
    the last answer sitting in the box looking like a fresh question."""
    state = {}
    monkeypatch.setattr(W.st, "session_state", state)

    state["gl_guess_pick"] = "Lebron James"
    state["gl_guess_narrow"] = 7
    state["gl_target"] = 12

    W.clear_player_picker("gl_guess")

    assert "gl_guess_pick" not in state
    assert "gl_guess_narrow" not in state
    assert state["gl_target"] == 12, "cleared more than its own keys"


def test_every_reset_in_the_app_clears_a_picker_through_the_widget():
    """The three reset paths must go through `clear_player_picker`. Reading
    the source is the only way to catch a fourth one written by hand."""
    import pathlib

    root = pathlib.Path(W.__file__).resolve().parent
    offenders = []
    for path in (root / "explore.py", root / "afl" / "game_lab.py"):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if "_guess_query" in line or "_guess_choice" in line:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        f"reset by hand-named picker keys: {offenders}")


def test_the_typeahead_list_is_capped_and_led_by_the_most_played(monkeypatch):
    """The browser filters this list as the user types, so it is shipped on
    every rerun -- the cap is what keeps that affordable, and the ordering
    is what keeps the cap from mattering."""
    import sports

    for sport in (sports.AFL, sports.NBA, sports.MLB, sports.NFL):
        if not sport.exists():
            continue
        quick = W.quick_player_options.__wrapped__(
            sport.key, sport.db, 0)
        assert 0 < len(quick) <= W.QUICK_PLAYER_LIMIT
        assert len({pid for pid, _, _ in quick}) == len(quick)
        # A nameless row would be a blank line in the dropdown. The NFL has
        # one, holding the plays its source could not attribute to anybody.
        assert all(name and str(name).strip() and player_label
                   for _, name, player_label in quick)
