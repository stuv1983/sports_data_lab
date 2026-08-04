"""Optional AFL family-draft relationship constraints.

``afl/load_family_draft.py`` imports Wikipedia's AFL father-son and AFLW
father-daughter tables into ``family_draft`` and resolves the drafted person
and father independently against the AFL ``players`` table.

Only trusted links are exposed to search and the Grid Solver.  Ambiguous and
unmatched source rows stay in the table for audit but never become answers.
Every public builder follows the project constraint contract:
``(sql_selecting_player_id, params)``.
"""

from __future__ import annotations

TRUSTED_STATUSES = ("unique", "resolved")

# Connection-local placeholder for clean/core-only databases.  It lets the
# parser and constraint registry exist without modifying a read-only database.
_PLACEHOLDER_SQL = """
CREATE TEMP TABLE IF NOT EXISTS family_draft (
    relationship_id INTEGER,
    source_row_id TEXT,
    competition TEXT,
    rule TEXT,
    draft_year INTEGER,
    drafted_player TEXT,
    club TEXT,
    father TEXT,
    drafted_player_id INTEGER,
    drafted_player_match_status TEXT,
    father_player_id INTEGER,
    father_match_status TEXT,
    imported_at TEXT
)
"""


def _main_table_exists(con) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='family_draft'"
    ).fetchone())


def _query_only(con) -> bool:
    """Whether SQLite currently rejects all writes, including TEMP writes."""
    row = con.execute("PRAGMA query_only").fetchone()
    return bool(row and row[0])


def ensure_family_draft_table(con) -> None:
    """Make family SQL safe before the optional import has been run.

    The Streamlit connection uses both ``mode=ro`` and ``PRAGMA query_only=ON``.
    SQLite treats ``CREATE TEMP TABLE`` as a write under ``query_only``, so the
    old placeholder strategy raised ``attempt to write a readonly database``
    for any father-son query before ``afl/load_family_draft.py`` had ever been run.
    On a query-only connection, simply leave the optional table absent:
    ``family_draft_available()`` already reports the dataset as not loaded, and
    Advanced Search turns that into a clean QuerySyntaxError rather than a
    crash.  This mirrors ensure_family_relationship_tables() in
    afl/family_relationships.py, which carries the same guard.
    """
    if not _main_table_exists(con):
        if _query_only(con):
            return
        con.execute(_PLACEHOLDER_SQL)


def family_draft_available(con) -> bool:
    """True when the persistent table has at least one trusted AFL child."""
    if not _main_table_exists(con):
        return False
    columns = {
        row[1] for row in con.execute("PRAGMA main.table_info(family_draft)")
    }
    required = {
        "competition", "rule", "draft_year", "club",
        "drafted_player_id", "drafted_player_match_status",
        "father_player_id", "father_match_status",
    }
    if not required <= columns:
        return False
    return bool(con.execute("""
        SELECT 1 FROM main.family_draft
        WHERE competition='AFL' AND rule='father-son'
          AND drafted_player_id IS NOT NULL
          AND drafted_player_match_status IN ('unique','resolved')
        LIMIT 1
    """).fetchone())


def family_draft_count(con) -> int:
    """Trusted AFL father-son drafted-player rows."""
    if not _main_table_exists(con):
        return 0
    return int(con.execute("""
        SELECT COUNT(*) FROM main.family_draft
        WHERE competition='AFL' AND rule='father-son'
          AND drafted_player_id IS NOT NULL
          AND drafted_player_match_status IN ('unique','resolved')
    """).fetchone()[0])


def father_son_selection():
    """Was drafted to the AFL under the father-son rule."""
    return ("""SELECT DISTINCT drafted_player_id FROM family_draft
               WHERE competition='AFL' AND rule='father-son'
                 AND drafted_player_id IS NOT NULL
                 AND drafted_player_match_status IN ('unique','resolved')""", [])


def father_also_played_afl():
    """Father-son draftee whose father also resolves to an AFL player."""
    return ("""SELECT DISTINCT drafted_player_id FROM family_draft
               WHERE competition='AFL' AND rule='father-son'
                 AND drafted_player_id IS NOT NULL
                 AND drafted_player_match_status IN ('unique','resolved')
                 AND father_player_id IS NOT NULL
                 AND father_match_status IN ('unique','resolved')""", [])


def father_played_for(club):
    """Father-son draftee whose linked father played for ``club``."""
    return ("""SELECT DISTINCT fd.drafted_player_id
               FROM family_draft fd
               JOIN games g ON g.player_id = fd.father_player_id
               WHERE fd.competition='AFL' AND fd.rule='father-son'
                 AND fd.drafted_player_id IS NOT NULL
                 AND fd.drafted_player_match_status IN ('unique','resolved')
                 AND fd.father_player_id IS NOT NULL
                 AND fd.father_match_status IN ('unique','resolved')
                 AND (LOWER(TRIM(g.club_now)) = LOWER(TRIM(?))
                      OR LOWER(TRIM(g.club_hist)) = LOWER(TRIM(?)))""",
            [club, club])


def parent_child_pair():
    """Either member of a trusted AFL father-son parent-child pair."""
    return ("""SELECT drafted_player_id AS player_id FROM family_draft
               WHERE competition='AFL' AND rule='father-son'
                 AND drafted_player_id IS NOT NULL
                 AND drafted_player_match_status IN ('unique','resolved')
                 AND father_player_id IS NOT NULL
                 AND father_match_status IN ('unique','resolved')
               UNION
               SELECT father_player_id AS player_id FROM family_draft
               WHERE competition='AFL' AND rule='father-son'
                 AND drafted_player_id IS NOT NULL
                 AND drafted_player_match_status IN ('unique','resolved')
                 AND father_player_id IS NOT NULL
                 AND father_match_status IN ('unique','resolved')""", [])


def child_of_father_id(father_player_id):
    """Trusted AFL father-son draftees linked to one database father ID."""
    return ("""SELECT DISTINCT drafted_player_id FROM family_draft
               WHERE competition='AFL' AND rule='father-son'
                 AND drafted_player_id IS NOT NULL
                 AND drafted_player_match_status IN ('unique','resolved')
                 AND father_player_id = ?
                 AND father_match_status IN ('unique','resolved')""",
            [int(father_player_id)])


def child_of_father_name(father):
    """Trusted AFL draftees whose source father name exactly matches."""
    return ("""SELECT DISTINCT drafted_player_id FROM family_draft
               WHERE competition='AFL' AND rule='father-son'
                 AND drafted_player_id IS NOT NULL
                 AND drafted_player_match_status IN ('unique','resolved')
                 AND LOWER(TRIM(father)) = LOWER(TRIM(?))""", [father])


FAMILY_DRAFT_BUILDERS = {
    "Father-son selection":       (father_son_selection, []),
    "Father also played AFL":     (father_also_played_afl, []),
    "Father played for club":     (father_played_for, ["club"]),
    "Parent-child pair":          (parent_child_pair, []),
}