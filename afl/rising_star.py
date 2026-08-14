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
    match_status TEXT,
    ineligible INTEGER
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


def rising_star_nominee_ineligible():
    """Nominated, but barred from winning because they were suspended.

    A nomination that could never become a win. The AFL lets a suspended
    player be nominated and then rules them out of the award, so this is a
    distinct thing to have happened to a career rather than a near-miss --
    which is what makes it worth asking about.

    Only Wikipedia's nomination table records it; see MERGED_FIELDS in
    utils/afl/load_rising_star.py for how the flag reaches rows that come
    from FootyWire.
    """
    return ("""SELECT DISTINCT player_id FROM rising_star_nominees
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL AND ineligible = 1""", [])


def rising_star_ineligible_available(con) -> bool:
    """Whether any ineligible nomination is actually linked to a player.

    Separate from rising_star_available: the nomination layer can be fully
    loaded from FootyWire alone, in which case this constraint would offer
    a question with no answers.
    """
    if not _main_table_exists(con):
        return False
    if not any(row[1] == "ineligible" for row in
               con.execute("PRAGMA main.table_info(rising_star_nominees)")):
        return False
    return bool(con.execute(
        "SELECT 1 FROM main.rising_star_nominees "
        "WHERE match_status IN ('unique','resolved') "
        "AND player_id IS NOT NULL AND ineligible = 1 LIMIT 1"
    ).fetchone())


RISING_STAR_BUILDERS = {
    "Rising Star nominee": (rising_star_nominee, []),
    "Rising Star nominee ineligible to win (suspension)": (
        rising_star_nominee_ineligible, []),
    "Rising Star nominee in season": (rising_star_nominee_in, ["season"]),
    "Rising Star nominee between seasons": (
        rising_star_nominee_between, ["from", "to"]),
    "Rising Star nominee for club": (rising_star_nominee_for, ["club"]),
    "Rising Star nominee for club between seasons": (
        rising_star_nominee_for_between, ["club", "from", "to"]),
}
