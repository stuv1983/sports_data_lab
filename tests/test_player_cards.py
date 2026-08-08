"""Player cards surface the rich career data already loaded per sport."""

import sqlite3
from pathlib import Path

import pytest

import explore
import sports


def _live(sport, player):
    if not sport.exists():
        pytest.skip(f"no built {sport.key} database")
    con = sqlite3.connect(sport.db)
    row = con.execute(
        f"SELECT {sport.schema.player_id} FROM {sport.schema.players} "
        f"WHERE {sport.schema.player}=?", (player,)
    ).fetchone()
    if row is None:
        con.close()
        pytest.skip(f"{player} is not in the built {sport.key} database")
    return con, row[0], explore._db_revision(sport.db)


def test_dustin_martin_card_has_titles_draft_votes_and_major_honours():
    con, pid, revision = _live(sports.AFL, "Dustin Martin")
    try:
        extra = explore._player_card_enrichment("afl", pid, revision, con)
        honours = {row["Honour"]: row["Times"] for row in extra["honours"]}
        assert explore._titles_won(sports.AFL, con, pid, revision) == 3
        assert ("Brownlow votes", "213") in extra["metrics"]
        assert extra["bio"] == [
            ("Draft", "Pick 3 · 2009 · National · Richmond")]
        assert honours["Norm Smith Medal"] == 3
        assert honours["All-Australian"] == 4
        logos = explore._player_card_logos(sports.AFL, con, "Richmond")
        assert [label for label, _path in logos] == [
            "AFL Data Lab", "Richmond"
        ]
        assert all(Path(path).is_file() for _label, path in logos)
    finally:
        con.close()


def test_card_blurb_uses_loaded_scores_titles_and_honours():
    text = explore._career_blurb(
        sports.AFL.vocab, 2010, 2024, 302, 1, 2009,
        career_score=338, titles=3,
        honours=[{"Honour": "All-Australian", "Times": 4}],
    )

    assert text.startswith("Drafted in 2009. Across 302 games")
    assert "338 goals" in text
    assert "3 premierships" in text
    assert "4x All-Australian" in text


@pytest.mark.parametrize("sport", [sports.AFL, sports.NBA, sports.MLB, sports.NFL])
def test_every_sport_card_has_its_league_badge(sport):
    con = sqlite3.connect(sport.db)
    try:
        logos = explore._player_card_logos(sport, con, "")
        assert logos[0][0] == sport.label
        assert Path(logos[0][1]).name == f"{sport.key}.png"
    finally:
        con.close()


def test_nba_card_derives_series_championships_from_team_seasons():
    con, pid, revision = _live(sports.NBA, "Stephen Curry")
    try:
        assert explore._titles_won(sports.NBA, con, pid, revision) == 4
        labels = dict(explore._player_card_enrichment(
            "nba", pid, revision, con)["metrics"])
        assert int(labels["Assists"].replace(",", "")) > 7_000
    finally:
        con.close()


def test_mlb_and_nfl_cards_use_sport_specific_honours_and_draft_fields():
    con, pid, revision = _live(sports.MLB, "Mike Trout")
    try:
        extra = explore._player_card_enrichment("mlb", pid, revision, con)
        honours = {row["Honour"]: row["Times"] for row in extra["honours"]}
        assert honours["Most Valuable Player"] == 3
        assert honours["Silver Slugger"] >= 9
    finally:
        con.close()

    con, pid, revision = _live(sports.NFL, "Patrick Mahomes")
    try:
        extra = explore._player_card_enrichment("nfl", pid, revision, con)
        assert extra["bio"] == [("Draft", "Pick 10 · round 1 · 2017 · KC")]
        assert dict(extra["metrics"])["Passing yards"].startswith("41,")
    finally:
        con.close()
