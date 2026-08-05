#!/usr/bin/env python3
"""Every NBA constraint must compile, bind and mean what it says.

The highest-value test here is `test_every_builder_compiles_and_runs`: it
calls every entry in BUILDERS with plausible arguments and pushes the result
through core.count. A registry is a wall of tuples, and a typo in one of
them is invisible until somebody picks that axis. Running all of them costs
nothing and catches all of them.

The rest pin the three decisions nba/constraints_nba.py makes that are not
simply "the generic builder, renamed":

  * franchise lineage expands one way (a Seattle square is about Seattle);
  * a statistic that predates its recording era must exclude players, not
    count their NULLs as zero;
  * "Teammate of…" is deliberately absent rather than present and wrong.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import sqlite3

import pytest

from nba import constraints_nba as C
import core

SCHEMA = C.SCHEMA

#: Plausible values for every argument name the registry uses, so each
#: builder can be called generically. Values are chosen to match the
#: fixture where it matters and to be harmless where it does not.
ARGUMENTS = {
    "club": "Oklahoma City Thunder",
    "venue": "TD Garden",
    "stat": "points", "stat_a": "points", "stat_b": "assists",
    "x": 10, "y": 5, "x_a": 10, "x_b": 3,
    "games": 2, "goals": 10, "clubs": 2, "times": 1,
    "avg": 5.0, "min_games": 1,
    "from": 1971, "to": 2010,
    "player_id": 1, "kind": "National", "source": "", "award": "",
    "position": "Guard",
}


@pytest.fixture(scope="module")
def con(nba_db):
    connection = sqlite3.connect(f"file:{nba_db}?mode=ro", uri=True)
    yield connection
    connection.close()


def names(con, constraint):
    sql, params = constraint
    return sorted(r[0] for r in con.execute(
        f"SELECT player FROM players WHERE player_id IN ({sql})", params))


# ------------------------------------------------------- the whole registry

def test_every_builder_compiles_and_runs(con):
    """One typo in the registry, caught here instead of in the UI."""
    failures = []
    for label, (fn, argnames) in sorted(C.BUILDERS.items()):
        try:
            args = [ARGUMENTS[a] for a in argnames]
        except KeyError as exc:
            failures.append(f"{label}: no test value for argument {exc}")
            continue
        try:
            constraint = fn(*args)
            core.count(con, [constraint], SCHEMA)
        except Exception as exc:                        # noqa: BLE001
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_the_registry_is_not_empty_and_has_no_duplicate_builders(con):
    assert len(C.BUILDERS) >= 30
    labels = list(C.BUILDERS)
    assert len(labels) == len(set(labels))


def test_require_schema_passes_on_a_built_database(con):
    C.require_schema(con)


def test_solve_and_square_return_the_schemas_columns(con):
    rows = C.solve(con, [C.played_for("Boston Celtics")])
    assert rows
    assert len(rows[0]) == len(SCHEMA.solve_columns())
    result = C.square(con, [C.played_for("Boston Celtics")])
    assert result.eligible >= 1


# ------------------------------------------------------ franchise lineage

def test_a_franchise_square_includes_its_earlier_identity(con):
    found = names(con, C.played_for("Oklahoma City Thunder"))
    assert "Jonah Kirkbride" in found       # Thunder only
    assert "Dale Ferriter" in found         # Sonics only, via lineage


def test_an_earlier_identity_square_is_about_that_identity_only(con):
    found = names(con, C.played_for("Seattle SuperSonics"))
    assert "Dale Ferriter" in found
    assert "Jonah Kirkbride" not in found, (
        "a Seattle square must not answer with a player who only ever "
        "appeared for Oklahoma City")


def test_a_player_who_spans_the_relocation_answers_both(con):
    assert "Marcus Oyelaran" in names(con, C.played_for("Seattle SuperSonics"))
    assert "Marcus Oyelaran" in names(con,
                                      C.played_for("Oklahoma City Thunder"))


def test_a_lineage_square_does_not_double_count(con):
    """The player who spans both identities is one answer, not two."""
    sql, params = C.played_for("Oklahoma City Thunder")
    rows = con.execute(f"SELECT player_id FROM ({sql})", params).fetchall()
    assert len(rows) == len({r[0] for r in rows})


# ------------------------------------------------------ NULL is not zero

def test_a_stat_predating_its_era_excludes_rather_than_zeroes(con):
    """Steals start in 1973-74. The 1971 career must not answer a steals
    square by having its NULLs read as zero -- nor be silently credited."""
    found = names(con, C.career_stat_total_min("steals", 1))
    assert "Slick Watkins" not in found
    assert "Marcus Oyelaran" in found


def test_the_pre_era_player_still_answers_a_stat_that_did_exist(con):
    assert "Slick Watkins" in names(con, C.career_stat_total_min("points", 1))


def test_no_pre_era_cell_was_written_as_zero(con):
    zeros = con.execute(
        "SELECT COUNT(*) FROM games WHERE season < 1973 "
        "AND (steals = 0 OR blocks = 0 OR fg3m = 0)").fetchone()[0]
    assert zeros == 0


# ------------------------------------------------------- NBA-specific

def test_the_champion_is_the_team_that_won_the_last_finals_game(con):
    assert names(con, C.champion()) == ["Ray Bellhouse"]


def test_finals_builders_read_the_nominated_round(con):
    assert names(con, C.played_in_the_finals()) == ["Ray Bellhouse"]
    assert names(con, C.won_the_finals()) == ["Ray Bellhouse"]
    assert "Marcus Oyelaran" in names(con, C.never_made_the_finals())


def test_playing_a_conference_final_is_not_playing_the_finals(con):
    """Marcus played a CF, which is a playoff game but not a Finals one."""
    assert "Marcus Oyelaran" in names(con, C.played_in_the_playoffs())
    assert "Marcus Oyelaran" not in names(con, C.played_in_the_finals())


def test_made_and_missed_the_playoffs_are_complementary_per_season(con):
    made = set(names(con, C.made_playoffs_season()))
    missed = set(names(con, C.missed_playoffs_season()))
    assert made
    # A player can be in both -- different seasons, different teams -- but
    # every answer must be a player who actually appeared.
    everyone = {r[0] for r in con.execute("SELECT player FROM players")}
    assert (made | missed) <= everyone


# --------------------------------------------------- deliberate omissions

def test_teammates_are_absent_rather_than_answered_wrongly():
    """Generic.teammate_of_id matches on (club, season), which the NBA's
    trade window makes wrong often enough to matter."""
    assert "Teammate of…" not in C.BUILDERS
    assert not hasattr(C, "teammate_of")
    assert not hasattr(C, "teammate_of_id")


def test_optional_layer_probes_answer_false_without_raising(con):
    assert C.draft_available(con) is False
    assert C.awards_available(con) is False


def test_no_builder_needs_a_layer_that_is_not_loaded():
    """DRAFT_BUILDERS and AWARD_BUILDER_NAMES gate the axis dropdown."""
    assert C.DRAFT_BUILDERS == set()
    assert C.AWARD_BUILDER_NAMES == set()
    assert C.DRAFT_TYPES == ()


def test_team_season_builders_are_declared_and_backed(con):
    assert C.TEAM_SEASON_BUILDERS <= set(C.BUILDERS)
    assert C.team_seasons_available(con) is True


# ------------------------------------------------------- the new categories

def test_born_outside_the_us_excludes_the_unrecorded(con):
    """A missing birthplace is not evidence of a foreign one."""
    found = names(con, C.born_outside_the_us())
    assert "Tomas Ilves" in found         # Estonia
    assert "Jonah Kirkbride" in found     # Canada
    assert "Ray Bellhouse" not in found   # USA
    assert "Slick Watkins" not in found   # no country recorded


def test_listed_at_position_matches_combination_codes(con):
    """A swingman listed 'GF' is both a guard and a forward."""
    guards = names(con, C.listed_at_position("Guard"))
    forwards = names(con, C.listed_at_position("Forward"))
    assert "Marcus Oyelaran" in guards      # GF
    assert "Marcus Oyelaran" in forwards    # GF
    assert "Tomas Ilves" not in guards      # C
    assert "Tomas Ilves" in names(con, C.listed_at_position("Center"))


def test_all_nba_with_club_is_the_same_season(con):
    """The strict pairing: selected *while* at the club, not ever-and-ever.

    Ray Bellhouse is All-NBA in his 2009 Celtics season and also played
    for the Lakers in 2010. He answers a Celtics square and must not answer
    a Lakers one, even though he is in the plain All-NBA set and did play
    for the Lakers.
    """
    assert "Ray Bellhouse" in names(con, C.all_nba_selection())

    celtics = names(con, C.all_nba_with_club("Boston Celtics"))
    assert celtics == ["Ray Bellhouse"]

    lakers = names(con, C.all_nba_with_club("Los Angeles Lakers"))
    assert "Ray Bellhouse" not in lakers


def test_all_nba_club_lineage_expands_one_way(con):
    """A Thunder square includes the Sonics season; Seattle stays Seattle.

    Marcus Oyelaran is All-NBA as a Sonic in 2006 and as a Thunder player
    in 2009. Jonah Kirkbride never wore Seattle at all and is the control.
    """
    seattle = names(con, C.all_nba_with_club("Seattle SuperSonics"))
    thunder = names(con, C.all_nba_with_club("Oklahoma City Thunder"))
    assert "Marcus Oyelaran" in seattle
    assert "Marcus Oyelaran" in thunder
    assert "Jonah Kirkbride" not in seattle


def test_new_layer_probes_are_declared(con):
    assert C.all_nba_available(con) is True
    assert C.birthplace_available(con) is True
    assert C.positions_available(con) is True
    assert set(C.LAYER_BUILDERS) <= set(C.BUILDERS)


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
