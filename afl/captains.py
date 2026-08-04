"""AFL club-captain constraints and availability helpers.

The table is optional because the core AFL source does not contain reliable
club captaincy.  ``afl/load_captains.py`` creates and links the table from a
separate scrape.  Only rows resolved to exactly one database player are
visible to search, the grid solver or Game Lab.

Every public constraint follows the project contract:
``(sql_selecting_player_id, params)``.
"""

TRUSTED_STATUSES = ("unique", "resolved")

# A TEMP placeholder lets the parser safely recognise captain criteria before
# the optional dataset has been imported.  It is connection-local and does not
# modify a read-only database.  The UI still hides the builders until trusted
# main-table rows exist.
_PLACEHOLDER_SQL = """
CREATE TEMP TABLE IF NOT EXISTS captaincies (
    captaincy_id INTEGER,
    season INTEGER,
    club TEXT,
    player TEXT,
    role TEXT,
    source_url TEXT,
    source_name TEXT,
    player_id INTEGER,
    match_status TEXT,
    candidate_count INTEGER,
    notes TEXT,
    imported_at TEXT
)
"""


def _main_table_exists(con) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='captaincies'"
    ).fetchone())


def ensure_captain_table(con) -> None:
    """Ensure captain SQL is safe on a database without the optional layer."""
    if not _main_table_exists(con):
        con.execute(_PLACEHOLDER_SQL)


def captain_count(con) -> int:
    """Trusted captain appointments, for the Database status panel."""
    if not _main_table_exists(con):
        return 0
    return con.execute(
        "SELECT COUNT(*) FROM main.captaincies "
        "WHERE match_status IN ('unique','resolved') "
        "AND player_id IS NOT NULL"
    ).fetchone()[0]


def captain_available(con) -> bool:
    """True only when the persistent table has the expected linked schema."""
    if not _main_table_exists(con):
        return False
    columns = {r[1] for r in con.execute("PRAGMA main.table_info(captaincies)")}
    if not {"season", "club", "player_id", "match_status"} <= columns:
        return False
    return bool(con.execute(
        "SELECT 1 FROM main.captaincies "
        "WHERE match_status IN ('unique','resolved') "
        "AND player_id IS NOT NULL LIMIT 1"
    ).fetchone())


def club_captain():
    """Was an AFL/VFL club captain in at least one recorded season."""
    return ("""SELECT DISTINCT player_id FROM captaincies
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL""", [])


def captain_of(club):
    """Captained a particular historical or current club identity."""
    return ("""SELECT DISTINCT player_id FROM captaincies
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL
                 AND LOWER(TRIM(club)) = LOWER(TRIM(?))""", [club])


def captain_between(lo, hi):
    """Was a club captain in any season in the inclusive range."""
    lo, hi = sorted((int(lo), int(hi)))
    return ("""SELECT DISTINCT player_id FROM captaincies
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL
                 AND season BETWEEN ? AND ?""", [lo, hi])


def captain_of_between(club, lo, hi):
    """Captained a club during an inclusive season range."""
    lo, hi = sorted((int(lo), int(hi)))
    return ("""SELECT DISTINCT player_id FROM captaincies
               WHERE match_status IN ('unique','resolved')
                 AND player_id IS NOT NULL
                 AND LOWER(TRIM(club)) = LOWER(TRIM(?))
                 AND season BETWEEN ? AND ?""", [club, lo, hi])


CAPTAIN_BUILDERS = {
    "Club captain": (club_captain, []),
    "Captain of club": (captain_of, ["club"]),
    "Club captain between seasons": (captain_between, ["from", "to"]),
    "Captain of club between seasons": (captain_of_between,
                                        ["club", "from", "to"]),
}
