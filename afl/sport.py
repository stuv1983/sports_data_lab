"""
afl/sport.py -- The AFL entry in the sports registry.

Its database, the core.Schema naming its columns, the words the UI puts on
screen, and which capabilities it has. sports.py collects this and the other
three; nothing here knows about them.
"""

import core
from data_paths import sport_db
from registry import Scoreline, Sport, Vocab

from .obscurity_model import MODEL

STATS = ["disposals", "kicks", "handballs", "marks", "goals", "behinds",
             "tackles", "hitouts", "inside50s", "clearances", "rebounds",
             "contested", "contested_marks", "marks_i50", "one_percenters",
             "bounces", "goal_assists", "brownlow", "frees_for", 
             "frees_against", "clangers", "uncontested"]

CLUB_LINEAGE = {
    "Brisbane Lions": ["Brisbane Lions", "Brisbane Bears", "Fitzroy"],
    "Sydney": ["Sydney", "South Melbourne"],
    "Western Bulldogs": ["Western Bulldogs", "Footscray"],
    "North Melbourne": ["North Melbourne", "Kangaroos"],
}

CLUBS = ["Adelaide", "Brisbane Lions", "Carlton",
             "Collingwood", "Essendon", "Fitzroy", "Fremantle", "Geelong",
             "Gold Coast", "GWS", "Hawthorn", "Melbourne", "North Melbourne",
             "Port Adelaide", "Richmond", "St Kilda", "Sydney", "University",
             "West Coast", "Western Bulldogs"]

VENUE_ALIASES = {
    "marvel stadium": "Docklands", "marvel": "Docklands",
    "etihad stadium": "Docklands", "telstra dome": "Docklands",
    "docklands": "Docklands", "colonial stadium": "Docklands",
    "mcg": "M.C.G.", "m.c.g.": "M.C.G.", "melbourne cricket ground": "M.C.G.",
    "scg": "S.C.G.", "s.c.g.": "S.C.G.",
    "gabba": "Gabba", "kardinia park": "Kardinia Park",
    "gmhba stadium": "Kardinia Park", "skilled stadium": "Kardinia Park",
    "optus stadium": "Perth Stadium", "perth stadium": "Perth Stadium",
    "subiaco": "Subiaco", "adelaide oval": "Adelaide Oval",
    "football park": "Football Park", "aami stadium": "Football Park",
    "princes park": "Princes Park", "waverley": "Waverley Park",
    "victoria park": "Victoria Park", "windy hill": "Windy Hill",
    # Bellerive Oval has been renamed twice by sponsors; Gridley uses the
    # current name. Jiangwan hosted the 2017-19 China games.
    "ninja stadium": "Bellerive Oval", "blundstone arena": "Bellerive Oval",
    "bellerive": "Bellerive Oval", "bellerive oval": "Bellerive Oval",
    "jiangwan": "Jiangwan Stadium", "jiangwan stadium": "Jiangwan Stadium",
}

SCHEMA = core.Schema(
    career_score="career_goals",
    career_postseason="finals_played",
    game_score="goals",
    is_final="is_final",
    #: A box score reads left to right the way AFL Tables prints one --
    #: possession first, then scoring, then the contest -- which is a
    #: different order from STATS above, where the most-asked-for square
    #: comes first.
    box_score=(
        "kicks", "handballs", "disposals", "marks", "goals", "behinds",
        "tackles", "hitouts", "goal_assists", "inside50s", "clearances",
        "rebounds", "contested", "uncontested", "contested_marks",
        "marks_i50", "one_percenters", "bounces", "clangers", "frees_for",
        "frees_against", "brownlow",
    ),
    stats=STATS,
    clubs=CLUBS,
    club_lineage=CLUB_LINEAGE,
    venue_aliases=VENUE_ALIASES,
    rebuild_cmd="python -m afl.build_db",
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_goals", "Goals"),
        ("p.finals_played", "Finals"),
        ("p.clubs_hist", "Clubs"),
        ("p.obscurity", "Obscurity"),
    ),
)

SPORT = Sport(
    key="afl",
    label="AFL Data Lab",
    icon="🏉",
    db=sport_db("afl", "gridley.db"),
    module="afl.constraints",
    schema=SCHEMA,
    vocab=Vocab(),
    theme="afl",
    missing_db_hint=("No AFL database found at "
                     f"{sport_db('afl', 'gridley.db')}. "
                     "Run `python -m afl.build_db` first."),
    empty_hint=("Nothing satisfies both. Note that disposals, marks and "
                "tackles are not recorded before 1965 — no earlier player "
                "can have them."),
    stat_eras={"goals": 1897, "brownlow": 1931,
               "disposals": 1965, "kicks": 1965, "handballs": 1965,
               "marks": 1965, "frees_for": 1965, "frees_against": 1965,
               "behinds": 1965, "hitouts": 1966, "tackles": 1987,
               "inside50s": 1998, "clearances": 1998, "rebounds": 1998,
               "clangers": 1998,
               "contested": 1999, "contested_marks": 1999,
               "marks_i50": 1999, "one_percenters": 1999,
               "uncontested": 1999, "bounces": 1999,
               "goal_assists": 2003},
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available",
                     "Captain data": "captain_available",
                     "Rising Star": "rising_star_available",
                     "Brownlow voting": "brownlow_available",
                     "Match score audit": "match_scores_available",
                     "Player index audit": "player_index_available",
                     "Venue profiles": "venue_profiles_available"},
    obscurity_model=MODEL,
    club_data_table="clubs",
    family_probe="family_relationships_available",
    criterion_parser="afl.parse_criteria",
    grid_library=True,
    daily_grid_feed="utils.fetch_grids:fetch_gridley",
    daily_grid_site="https://gridleygame.com",
    game_lab_module="afl.game_lab",
    has_club_explorer=True,
    has_awards_page=True,
    awards_page_module="afl.awards_page",
    has_past_games=True,
    data_notes_module="afl.data_notes",
    ground_explorer_module="afl.ground_explorer",
    has_ground_explorer=True,
    draft_page_module="afl.draft_page",
    has_draft_page=True,
    has_draftguru_player_cards=True,
    past_games_hint=("Run `python -m utils.afl.fetch_club_sources`, then "
                     "`python -m utils.afl.load_club_all_games`."),
    #: A grand final is one match, so a win in it is a premiership.
    title_round="GF",
    loader_hints={
        "draft_available": "Run `python afl/load_draftguru.py`, then `python -m afl.link_draft`.",
        "awards_available": "Run `python afl/load_draftguru.py`, then `python -m afl.link_people`.",
        "captain_available": "Run `python -m utils.afl.load_captains` for club-captain data.",
        "rising_star_available": ("Run `afl/fetch_footywire_rising_star.py`, "
                                  "then `python -m utils.afl.load_rising_star`."),
        "brownlow_available": "Run `python -m utils.afl.load_brownlow`.",
        "match_scores_available": (
            "Cache AFL Tables bg3.txt, then run `python -m utils.afl.load_match_scores "
            "--source data/afl/raw/matches/afltables_bg3.txt`."),
        "player_index_available": (
            "Run `python -m afl.player_index_audit --fetch` for the low-rate "
            "AFL Tables identity audit."),
        "venue_profiles_available": (
            "Run `python -m utils.afl.load_venues --fetch` for the low-rate AFL "
            "Tables venue archive."),
    },
    club_data_tables=frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records",
    }),
    club_data_hint=("Run `python -m utils.afl.fetch_club_sources`, then "
                    "`python -m utils.afl.load_club_sources` for Club Explorer."),
    family_hint=("Run `afl/scrape_wikipedia_families.py`, then "
                 "`python -m utils.afl.load_family_relationships` for family links."),
    search_extension_modules=("afl.search_tokens",),
    #: The tables Advanced Search's query builder exposes: the analytical
    #: layers a reader would ask about, not the scrape indexes, staging
    #: snapshots, link tables and issue logs that share the file.
    query_tables=(
        "players", "games", "matches", "match_details", "clubs",
        "captaincies", "draft", "brownlow_results", "brownlow_round_votes",
        "all_australian", "all_australian_history", "awards",
        "hall_of_fame", "rising_star_nominees", "season_goals",
        "team_seasons", "team_selections", "family_relationships",
        "venue_summary", "venue_match_records", "venue_player_records",
        "venue_player_game_records", "venue_team_records",
        "club_player_register", "club_player_totals",
        "club_player_records", "club_player_averages", "historic_grids",
    ),
    #: The build declares dates as TEXT and flags as INTEGER; the query
    #: builder needs the application-level truth to offer the right
    #: operators.
    query_column_kinds={
        # dob columns are declarable again: build_db and the round loader
        # write ISO through utils/afl/dob.canonical_dob, and
        # utils/afl/normalize_dob.py rewrote the mixed spellings already
        # stored. tests/test_query_metadata.py holds the format contract.
        "players.dob": "date",
        "games.date": "date",
        "games.dob": "date",
        "games.birth_est": "date",
        "games.is_home": "boolean", "games.is_final": "boolean",
        "matches.match_date": "date", "matches.is_final": "boolean",
        "match_details.scheduled_datetime": "datetime",
        "clubs.updated_at": "datetime",
        "venue_match_records.match_date": "date",
        "club_player_register.dob": "date",
        "club_player_records.match_date": "date",
        "historic_grids.date": "date",
        "all_australian.is_captain": "boolean",
        "all_australian.is_vice_captain": "boolean",
        "hall_of_fame.is_legend": "boolean",
        "rising_star_nominees.is_season_winner": "boolean",
        "season_goals.is_club_leading": "boolean",
    },
    #: Text columns worth a distinct-values scan when the visual tree
    #: renders, so they become select widgets. An explicit allowlist, not
    #: a guess: profiling every text column of a wide table cost seconds
    #: per cold render, mostly on names and dates no select could hold.
    query_low_cardinality_columns=(
        "games.club_now", "games.club_hist", "games.opponent",
        "games.result", "games.round", "games.venue",
    ),
    search_examples=(
        'club:Hawthorn games>=200 sort:obscurity',
        'game.disposals>=40 postseason:true',
        'season.goals>=50 debut:1990..1999',
        'career.marks>=1000 avg.disposals>=20',
        'award:brownlow-medal',
        'club:Fitzroy club_any:Carlton',
    ),
    #: A goal is six points and a hundred is the century, and neither
    #: number is anywhere in the match rows -- they come from the code of
    #: football, so the sport states them rather than the page guessing.
    scorelines=(
        Scoreline(
            "Decided by under a goal",
            "A goal is six points, so a margin of five or fewer means one "
            "kick would have changed the result.",
            max_margin=5),
        Scoreline(
            "Under a goal, both past 100",
            "The close one that was also a shootout: five points or fewer "
            "between them, and neither side under a hundred.",
            max_margin=5, min_low_score=100),
        Scoreline(
            "Both sides past 100",
            "Every game in which the losing side still made three figures.",
            min_low_score=100),
        Scoreline("Drawn", "No margin at all.", max_margin=0),
        Scoreline(
            "Beaten by 100 or more",
            "The hundred-point thrashing, either way round.",
            min_margin=100),
        Scoreline(
            "Neither side reached 50",
            "The low-scoring arm wrestle — wet decks, old grounds, and the "
            "modern game's rare shutdowns.",
            max_high_score=49),
        Scoreline(
            "250 points between them",
            "The highest-scoring games ever played; the record is 345.",
            min_total=250),
    ),
    grid_defaults=(
        (("Played for club", {"club": "St Kilda"}),
         ("Played for club", {"club": "North Melbourne"}),
         ("150+ / X+ career games", {"games": 150})),
        (("X+ goals at 2+ clubs", {"goals": 30, "clubs": 2}),
         ("Teammate of…", {"player": "Mason Wood"}),
         ("No finals wins (played finals)", {})),
    ),
    venue_display={
        "Docklands": "Marvel Stadium",
        "Kardinia Park": "GMHBA Stadium",
        "Perth Stadium": "Optus Stadium",
        "Football Park": "AAMI Stadium",
        "M.C.G.": "MCG",
        "S.C.G.": "SCG",
        "Western Oval": "Whitten Oval",
        "Princes Park": "Optus Oval",
        "Carrara": "People First Stadium",
        "Sydney Showground": "ENGIE Stadium",
        "York Park": "UTAS Stadium",
        "Manuka Oval": "Manuka",
    },
)
