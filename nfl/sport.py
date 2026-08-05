"""
nfl/sport.py -- The NFL entry in the sports registry.
"""

import core
from data_paths import sport_db
from registry import Sport, Vocab

from . import nfl_reference
from .obscurity_model import MODEL

#
# The database is built by the standalone build_nfl_db.py from nflverse via
# nflreadpy, and then adapted to this schema by nfl/patch_nfl_db.py, which
# derives the columns core.py needs -- club names, venue, result, is_playoff,
# career_game_no and a touchdown total -- and measures the statistic list
# below. Until that patch has run, require_schema declines with the command
# to run, rather than the app half-working.
#
# One row of `games` is a player's week, and the weekly data starts in 1999.
# Matches and rosters reach back to 1920, so the database knows a player was
# rostered in 1955 while having no game for him: every career total here is
# a 1999-onward figure.

STATS = nfl_reference.stats()

CLUBS = nfl_reference.teams()

SCHEMA = core.Schema(
    #: Career columns build_nfl_db.py writes on `players`. It names the
    #: club-history columns teams_hist / n_teams rather than reusing the
    #: AFL's, so the schema maps them here rather than the build renaming
    #: them.
    career_score="career_touchdowns",
    career_postseason="career_postseason_games",
    clubs_hist="teams_hist",
    n_clubs="n_teams",
    game_score="touchdowns",
    is_final="is_playoff",
    opponent="opponent_team",
    stats=STATS,
    clubs=CLUBS,
    club_lineage=nfl_reference.club_lineage(),
    venue_aliases=nfl_reference.venue_aliases(),
    rebuild_cmd="python .\\build_nfl_db.py --all-history, then "
                "python -m nfl.patch_nfl_db",
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_touchdowns", "Touchdowns"),
        ("p.career_postseason_games", "Playoffs"),
        ("p.teams_hist", "Teams"),
        ("p.obscurity", "Obscurity"),
    ),
)

SPORT = Sport(
    key="nfl",
    label="NFL Data Lab",
    icon="🏈",
    db=sport_db("nfl"),
    module="nfl.constraints_nfl",
    schema=SCHEMA,
    vocab=Vocab(club="team", clubs="teams", game="game", games="games",
                season="season", venue="stadium", venues="stadiums",
                score="touchdowns", postseason="playoffs",
                postseason_one="playoff game", title="Super Bowl",
                grid_source="Immaculate Grid"),
    theme="nfl",
    build_cmd="python .\\build_nfl_db.py --all-history",
    preview=True,
    missing_db_hint=("No NFL database found at "
                     f"{sport_db('nfl')}. Build it with "
                     "`python .\\build_nfl_db.py --all-history`, then run "
                     "`python -m nfl.patch_nfl_db` to add the columns the "
                     "app reads."),
    empty_hint=("Nothing satisfies both. Note that nflverse's weekly player "
                "statistics begin in 1999: no player has a game, a career "
                "total or a touchdown here for a season before that, even "
                "where the schedule and the rosters go back to 1920."),
    stat_eras=nfl_reference.stat_eras(),
    #: A Super Bowl is one game, so a win in it is the championship.
    title_round="SB",
    #: Every dataset the build imports gets a status row. The eight
    #: `--extended` ones are optional in the builder -- one can fail and be
    #: recorded in build_warnings while the build succeeds -- so "not
    #: loaded" is a real answer the panel has to be able to give.
    optional_layers={"Draft data": "draft_available",
                     "Rosters (1920 on)": "rosters_available",
                     "Weekly rosters (2002 on)": "rosters_weekly_available",
                     "Snap counts (2012 on)": "snap_counts_available",
                     "Injuries (2009 on)": "injuries_available",
                     "Depth charts (2001 on)": "depth_charts_available",
                     "Officials (2015 on)": "officials_available",
                     "Combine": "combine_available",
                     "Contracts": "contracts_available",
                     "Trades": "trades_available"},
    loader_hints={
        probe: "Rebuild with `python build_nfl_db.py --all-history "
               "--extended --replace`, then `python -m nfl.patch_nfl_db`."
        for probe in ("rosters_weekly_available", "snap_counts_available",
                      "injuries_available", "depth_charts_available",
                      "officials_available", "combine_available",
                      "contracts_available", "trades_available")
    },
    obscurity_model=MODEL,
    has_club_explorer=True,
    club_data_table="clubs",
    club_data_tables=frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records",
    }),
    club_data_hint=("Run `python utils/derive_club_tables.py --sport nfl` "
                    "for Team Explorer."),
    #: nflverse's player list is every known identity, most of whom retired
    #: before the weekly statistics begin. A player with no game is not an
    #: answer to any square, and scoring him is scoring an absence.
    obscurity_population="career_games >= 1",
    search_examples=(
        'club:"Kansas City Chiefs" games>=100 sort:obscurity',
        'game.passing_yards>=400 postseason:true',
        'season.receiving_yards>=1500 debut:2000..2009',
        'career.rushing_yards>=10000',
        'club:"Las Vegas Raiders" club_any:"Los Angeles Chargers"',
        'played:1999..2005 sort:fewest_games',
    ),
    grid_defaults=(
        (("Played for club", {"club": "Kansas City Chiefs"}),
         ("Played for club", {"club": "New England Patriots"}),
         ("150+ / X+ career games", {"games": 100})),
        (("X+ goals at 2+ clubs", {"goals": 10, "clubs": 2}),
         ("X+ of a stat in one season", {"stat": "receiving_yards",
                                         "x": 1000}),
         ("Won a Super Bowl", {})),
    ),
)
