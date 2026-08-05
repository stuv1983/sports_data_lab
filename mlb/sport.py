"""
mlb/sport.py -- The MLB entry in the sports registry.
"""

import core
from data_paths import sport_db
from registry import Sport, Vocab

from . import mlb_reference
from .obscurity_model import MODEL

#
# A row of the MLB `games` table is a player's *season* with one team, not
# a game: Lahman has no box scores. constraints_mlb.py declines the
# per-game squares rather than answering them from season totals.

STATS = ["home_runs", "hits", "rbis", "stolen_bases", "walks",
             "strikeouts", "at_bats", "runs", "doubles", "triples",
             "wins", "losses", "era", "saves"]

SCHEMA = core.Schema(
    career_score="career_home_runs",
    career_postseason="postseason_played",
    game_score="home_runs",
    is_final="is_postseason",
    stats=STATS,
    clubs=mlb_reference.teams(),
    club_lineage=mlb_reference.club_lineage(),
    venue_aliases=mlb_reference.venue_aliases(),
    rebuild_cmd="python -m mlb.build_mlb_db",
    #: A row is a season, so career_games is SUM(games), not COUNT(*), and
    #: the career batting totals are regular season -- which is what the
    #: sport means by a career home-run count.
    career_totals_sql={
        "career_games": ("SUM(g.games)", "g.is_postseason = 0"),
        "career_home_runs": ("SUM(g.home_runs)", "g.is_postseason = 0"),
        "postseason_played": ("SUM(g.games)", "g.is_postseason = 1"),
    },
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_home_runs", "Home Runs"),
        ("p.postseason_played", "Postseason"),
        ("p.clubs_hist", "Franchises"),
        ("p.obscurity", "Obscurity"),
    ),
)

SPORT = Sport(
    key="mlb",
    label="MLB Data Lab",
    icon="⚾",
    db=sport_db("mlb"),
    module="mlb.constraints_mlb",
    schema=SCHEMA,
    vocab=Vocab(club="franchise", clubs="franchises", game="game", games="games",
                season="season", venue="ballpark", venues="ballparks",
                score="home runs", postseason="postseason",
                postseason_one="postseason game", title="World Series",
                grid_source="Immaculate Grid"),
    theme="mlb",
    build_cmd="python -m mlb.build_mlb_db",
    missing_db_hint=(f"No MLB database found at {sport_db('mlb')}. "
                     "Run `python -m mlb.build_mlb_db` first."),
    empty_hint=("Nothing satisfies both. Note that a row here is a player's "
                "season with one team rather than a game -- Lahman has no "
                "box scores, so there are no single-game squares -- and "
                "that 19th-century seasons are missing many RBI, stolen "
                "base and strikeout lines even though the columns exist."),
    stat_eras=mlb_reference.stat_eras(),
    games_row_label="Player-seasons",
    optional_layers={"Awards data": "awards_available",
                     "Hall of Fame": "hall_of_fame_available"},
    obscurity_model=MODEL,
    search_examples=(
        'club:"New York Yankees" games>=1000 sort:obscurity',
        'season.home_runs>=40 debut:1990..1999',
        'career.hits>=3000',
        'club:"Brooklyn Dodgers" club_any:"Los Angeles Dodgers"',
        'played:1946..1955 sort:fewest_games',
    ),
    grid_defaults=(
        (("Played for club", {"club": "New York Yankees"}),
         ("Played for club", {"club": "Boston Red Sox"}),
         ("150+ / X+ career games", {"games": 1000})),
        (("X+ goals at 2+ clubs", {"goals": 100, "clubs": 2}),
         ("X+ of a stat in one season", {"stat": "home_runs", "x": 40}),
         ("No finals wins (played finals)", {})),
    ),
)
