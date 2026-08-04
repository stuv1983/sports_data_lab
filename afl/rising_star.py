"""AFL Rising Star nominee constraints and optional-table helpers."""

TRUSTED_STATUSES = ("unique", "resolved")

_PLACEHOLDER_SQL = """
CREATE TEMP TABLE IF NOT EXISTS rising_star_nominees (
    source_key TEXT,
    season INTEGER,
    nomination_round TEXT,
    round_number INTEGER,
    player TEXT,
    club TEXT,
    player_id INTEGER,
    match_status TEXT
)
"""


def _main_table_exists(con) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='rising_star_nominees'"
    ).fetchone())


def ensure_rising_star_table(con) -> None:
    """Keep SQL safe when the optional nomination layer is not loaded."""
    if not _main_table_exists(con):
        con.execute(_PLACEHOLDER_SQL)


def rising_star_available(con) -> bool:
    if not _main_table_exists(con):
        return False
    return bool(con.execute(
        "SELECT 1 FROM main.rising_star_nominees "
        "WHERE match_status IN ('unique','resolved') "
        "AND player_id IS NOT NULL LIMIT 1"
    ).fetchone())


def rising_star_count(con) -> int:
    """Trusted nomination links, for the Database status panel."""
    if not _main_table_exists(con):
        return 0
    return con.execute(
        "SELECT COUNT(*) FROM main.rising_star_nominees "
        "WHERE match_status IN ('unique','resolved') "
        "AND player_id IS NOT NULL"
    ).fetchone()[0]


def rising_star_nominee():
    return ("""SELECT DISTINCT player_id FROM rising_star_nominees
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL""", [])


def rising_star_nominee_in(season):
    return ("""SELECT DISTINCT player_id FROM rising_star_nominees
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL AND season = ?""", [int(season)])


def rising_star_nominee_between(lo, hi):
    lo, hi = sorted((int(lo), int(hi)))
    return ("""SELECT DISTINCT player_id FROM rising_star_nominees
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL AND season BETWEEN ? AND ?""",
            [lo, hi])


def rising_star_nominee_for(club):
    return ("""SELECT DISTINCT player_id FROM rising_star_nominees
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL
                 AND LOWER(TRIM(club)) = LOWER(TRIM(?))""", [club])


def rising_star_nominee_for_between(club, lo, hi):
    lo, hi = sorted((int(lo), int(hi)))
    return ("""SELECT DISTINCT player_id FROM rising_star_nominees
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL
                 AND LOWER(TRIM(club)) = LOWER(TRIM(?))
                 AND season BETWEEN ? AND ?""", [club, lo, hi])


RISING_STAR_BUILDERS = {
    "Rising Star nominee": (rising_star_nominee, []),
    "Rising Star nominee in season": (rising_star_nominee_in, ["season"]),
    "Rising Star nominee between seasons": (
        rising_star_nominee_between, ["from", "to"]),
    "Rising Star nominee for club": (rising_star_nominee_for, ["club"]),
    "Rising Star nominee for club between seasons": (
        rising_star_nominee_for_between, ["club", "from", "to"]),
}
