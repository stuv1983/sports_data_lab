"""
afl/sport.py -- The AFL entry in the sports registry.

Its database, the core.Schema naming its columns, the words the UI puts on
screen, and which capabilities it has. sports.py collects this and the other
three; nothing here knows about them.
"""

import core
from data_paths import sport_db
from registry import Sport, Vocab

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
}

SCHEMA = core.Schema(
    career_score="career_goals",
    game_score="goals",
    is_final="is_final",
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
                     "Match score audit": "match_scores_available"},
    obscurity_model=MODEL,
    club_data_table="clubs",
    family_probe="family_relationships_available",
    criterion_parser="afl.parse_criteria",
    grid_library=True,
    game_lab_module="afl.game_lab",
    has_club_explorer=True,
    has_awards_page=True,
    awards_page_module="afl.awards_page",
    has_past_games=True,
    past_games_hint=("Run `python utils/fetch_club_sources.py`, then "
                     "`python utils/load_club_all_games.py`."),
    #: A grand final is one match, so a win in it is a premiership.
    title_round="GF",
    loader_hints={
        "draft_available": "Run `afl/load_draftguru.py`, then `afl/link_draft.py`.",
        "awards_available": "Run `afl/load_draftguru.py`, then `afl/link_people.py`.",
        "captain_available": "Run `afl/load_captains.py` for club-captain data.",
        "rising_star_available": ("Run `afl/fetch_footywire_rising_star.py`, "
                                  "then `afl/load_rising_star.py`."),
        "brownlow_available": "Run `python -m afl.load_brownlow`.",
        "match_scores_available": (
            "Cache AFL Tables bg3.txt, then run `python -m afl.load_match_scores "
            "--source data/afl/raw/matches/afltables_bg3.txt`."),
    },
    club_data_tables=frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records",
    }),
    club_data_hint=("Run `utils/fetch_club_sources.py`, then "
                    "`utils/load_club_sources.py` for Club Explorer."),
    family_hint=("Run `afl/scrape_wikipedia_families.py`, then "
                 "`afl/load_family_relationships.py` for family links."),
    search_examples=(
        'club:Hawthorn games>=200 sort:obscurity',
        'game.disposals>=40 postseason:true',
        'season.goals>=50 debut:1990..1999',
        'career.marks>=1000 avg.disposals>=20',
        'award:brownlow-medal',
        'club:Fitzroy club_any:Carlton',
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
