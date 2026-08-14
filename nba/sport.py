"""
nba/sport.py -- The NBA entry in the sports registry.

CLUBS, the club lineage and the stat eras come from the reference file the
build writes, so they are measured rather than hand-maintained.
"""

import core
from data_paths import sport_db
from registry import Sport, Vocab

from . import nba_reference
from .obscurity_model import MODEL

STATS = ["points", "rebounds", "assists", "steals", "blocks",
             "turnovers", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
             "oreb", "dreb", "minutes", "plus_minus", "fouls"]

SCHEMA = core.Schema(
    career_score="career_points",
    career_postseason="playoffs_played",
    game_score="points",
    is_final="is_playoff",
    position="position",
    height="height_cm",
    weight="weight_kg",
    height_unit="cm",
    weight_unit="kg",
    stats=STATS,
    clubs=nba_reference.teams(),
    club_lineage=nba_reference.club_lineage(),
    venue_aliases=nba_reference.venue_aliases(),
    required_player_cols=("name_key", "career_minutes"),
    #: The NBA schedule dates a match in `date`; `match_date` is the AFL
    #: build's name for the same thing.
    match_date="date",
    match_facts=(("season_label", "Season"), ("phase", "Phase")),
    rebuild_cmd="python -m nba.build_nba_db",
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_points", "Points"),
        ("p.playoffs_played", "Playoffs"),
        ("p.clubs_hist", "Teams"),
        ("p.obscurity", "Obscurity"),
    ),
)

SPORT = Sport(
    key="nba",
    label="NBA Data Lab",
    icon="🏀",
    db=sport_db("nba"),
    module="nba.constraints_nba",
    schema=SCHEMA,
    vocab=Vocab(club="team", clubs="teams", game="game", games="games",
                season="season", venue="arena", venues="arenas",
                score="points", postseason="playoffs",
                postseason_one="playoff game", title="championship",
                grid_source="Immaculate Grid"),
    theme="nba",
    build_cmd="python -m nba.build_nba_db",
    preview=True,
    missing_db_hint=(f"No NBA database found at {sport_db('nba')}. "
                     "Build one with `python -m nba.build_nba_db --source csv`, "
                     "or `--source nba_api` once an API key is configured "
                     "(see config.py)."),
    empty_hint=("Nothing satisfies both. Steals, blocks, turnovers and the "
                "offensive/defensive rebound split are not recorded before "
                "1973-74, and three-pointers not before 1979-80."),
    stat_eras=nba_reference.stat_eras(),
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available"},
    loader_hints={
        "awards_available": ("Run `python -m utils.shared.load_wiki_awards "
                             "--sport nba --root <wiki-scrape-root>`.")
    },
    obscurity_model=MODEL,
    has_club_explorer=True,
    has_awards_page=True,
    awards_page_module="utils.shared.wiki_awards_page",
    has_ground_explorer=True,
    ground_explorer_module="nba.ground_explorer",
    has_past_games=True,
    past_games_hint=("Run `python -m utils.nba.load_club_history` to project the "
                     "schedule already in the database into club-history "
                     "rows. Add `--attendance <game_info.csv>` from the box "
                     "score dataset to fill in crowds before 2023."),
    club_data_table="clubs",
    club_data_tables=frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records",
    }),
    club_data_hint=("Run `python utils/derive_club_tables.py --sport nba` "
                    "for Team Explorer."),
    query_tables=(
        "players", "games", "matches", "clubs", "teams", "franchises",
        "arenas", "arena_teams", "player_seasons", "player_team_history",
        "team_seasons", "nba_all_nba", "wiki_awards",
        "club_player_register", "club_player_totals",
        "club_player_records",
    ),
    query_column_kinds={
        "games.date": "date", "games.is_home": "boolean",
        "games.is_playoff": "boolean", "matches.date": "date",
        "clubs.updated_at": "datetime",
        "club_player_register.dob": "date",
        "club_player_records.match_date": "date",
        "teams.is_current": "boolean",
        "franchises.is_active": "boolean",
        "team_seasons.made_playoffs": "boolean",
        "team_seasons.reached_finals": "boolean",
    },
    query_low_cardinality_columns=(
        "games.club_now", "games.club_hist", "games.opponent",
        "games.result", "games.venue", "games.season_label",
    ),
    search_examples=(
        'club:"Boston Celtics" games>=500 sort:obscurity',
        'game.points>=50 postseason:true',
        'season.points>=2000 debut:1980..1999',
        'club:"Seattle SuperSonics" club_any:"Oklahoma City Thunder"',
        'career.rebounds>=10000 avg.assists>=6',
        'played:1996..2005 sort:fewest_games',
    ),
    grid_defaults=(
        (("Played for club", {"club": "Boston Celtics"}),
         ("Played for club", {"club": "Los Angeles Lakers"}),
         ("150+ / X+ career games", {"games": 500})),
        (("X+ goals at 2+ clubs", {"goals": 1000, "clubs": 2}),
         ("X+ of a stat in one game", {"stat": "points", "x": 40}),
         ("No finals wins (played finals)", {})),
    ),
)
