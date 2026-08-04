"""Optional broad AFL/VFL family relationship constraints.

``afl/scrape_wikipedia_families.py`` extracts listed families and explicit
relationships. ``afl/load_family_relationships.py`` links each source member to the
local AFL ``players`` table. Only links resolved to exactly one database player
are visible here; ambiguous/unmatched rows remain available for audit.

The source distinguishes two concepts:

* same listed family -- useful even when the exact direct relationship is not
  safely machine-readable; and
* explicit relationships -- sibling, parent/child, cousin, grandparent, etc.

Every builder follows the project contract:
``(sql_selecting_player_id, params)``.
"""

from __future__ import annotations

TRUSTED_STATUSES = ("unique", "resolved")

_PLACEHOLDER_MEMBERS_SQL = """
CREATE TEMP TABLE IF NOT EXISTS family_members (
    family_member_id INTEGER,
    source_member_id TEXT,
    family_key TEXT,
    family_name TEXT,
    member_name TEXT,
    clubs_raw TEXT,
    player_id INTEGER,
    match_status TEXT,
    candidate_count INTEGER,
    match_notes TEXT,
    imported_at TEXT
)
"""

_PLACEHOLDER_RELATIONSHIPS_SQL = """
CREATE TEMP TABLE IF NOT EXISTS family_relationships (
    relationship_id INTEGER,
    source_relationship_id TEXT,
    family_key TEXT,
    family_name TEXT,
    person_a_source_member_id TEXT,
    person_a_name TEXT,
    person_a_role TEXT,
    person_b_source_member_id TEXT,
    person_b_name TEXT,
    person_b_role TEXT,
    relationship_type TEXT,
    relationship_label TEXT,
    evidence TEXT,
    extraction_method TEXT,
    confidence TEXT,
    imported_at TEXT
)
"""


def _main_table_exists(con, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM main.sqlite_master "
            "WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _query_only(con) -> bool:
    """Whether SQLite currently rejects all writes, including TEMP writes."""
    row = con.execute("PRAGMA query_only").fetchone()
    return bool(row and row[0])


def ensure_family_relationship_tables(con) -> None:
    """Keep optional family SQL safe without breaking read-only app startup.

    The Streamlit connection uses both ``mode=ro`` and ``PRAGMA query_only=ON``.
    SQLite treats ``CREATE TEMP TABLE`` as a write under ``query_only``, so the
    old placeholder strategy raised ``attempt to write a readonly database``
    before the app could even hide unavailable family builders.  On a
    query-only connection, simply leave the optional tables absent; availability
    checks and Advanced Search already report that the dataset is not loaded.
    """
    missing_members = not _main_table_exists(con, "family_members")
    missing_relationships = not _main_table_exists(con, "family_relationships")
    if not (missing_members or missing_relationships):
        return
    if _query_only(con):
        return
    if missing_members:
        con.execute(_PLACEHOLDER_MEMBERS_SQL)
    if missing_relationships:
        con.execute(_PLACEHOLDER_RELATIONSHIPS_SQL)


def family_relationships_available(con) -> bool:
    if not (
        _main_table_exists(con, "family_members")
        and _main_table_exists(con, "family_relationships")
    ):
        return False
    member_columns = {
        row[1] for row in con.execute("PRAGMA main.table_info(family_members)")
    }
    relationship_columns = {
        row[1]
        for row in con.execute("PRAGMA main.table_info(family_relationships)")
    }
    if not {
        "source_member_id", "family_key", "player_id", "match_status"
    } <= member_columns:
        return False
    if not {
        "person_a_source_member_id", "person_b_source_member_id",
        "relationship_type", "relationship_label",
    } <= relationship_columns:
        return False
    return bool(
        con.execute(
            """
            SELECT 1
            FROM main.family_members a
            JOIN main.family_members b
              ON b.family_key=a.family_key
             AND b.source_member_id<>a.source_member_id
            WHERE a.player_id IS NOT NULL
              AND b.player_id IS NOT NULL
              AND a.match_status IN ('unique','resolved')
              AND b.match_status IN ('unique','resolved')
            LIMIT 1
            """
        ).fetchone()
    )


def family_member_count(con) -> int:
    """Distinct trusted AFL players appearing in a multi-player family."""
    if not _main_table_exists(con, "family_members"):
        return 0
    return int(
        con.execute(
            """
            SELECT COUNT(DISTINCT a.player_id)
            FROM main.family_members a
            WHERE a.player_id IS NOT NULL
              AND a.match_status IN ('unique','resolved')
              AND EXISTS (
                  SELECT 1 FROM main.family_members b
                  WHERE b.family_key=a.family_key
                    AND b.source_member_id<>a.source_member_id
                    AND b.player_id IS NOT NULL
                    AND b.match_status IN ('unique','resolved')
              )
            """
        ).fetchone()[0]
    )


def trusted_relationship_count(con) -> int:
    if not (
        _main_table_exists(con, "family_members")
        and _main_table_exists(con, "family_relationships")
    ):
        return 0
    return int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM main.family_relationships r
            JOIN main.family_members a
              ON a.source_member_id=r.person_a_source_member_id
            JOIN main.family_members b
              ON b.source_member_id=r.person_b_source_member_id
            WHERE a.player_id IS NOT NULL
              AND b.player_id IS NOT NULL
              AND a.match_status IN ('unique','resolved')
              AND b.match_status IN ('unique','resolved')
            """
        ).fetchone()[0]
    )


def family_member():
    """Listed in a family with at least one other trusted AFL/VFL player."""
    return (
        """
        SELECT DISTINCT a.player_id
        FROM family_members a
        WHERE a.player_id IS NOT NULL
          AND a.match_status IN ('unique','resolved')
          AND EXISTS (
              SELECT 1 FROM family_members b
              WHERE b.family_key=a.family_key
                AND b.source_member_id<>a.source_member_id
                AND b.player_id IS NOT NULL
                AND b.match_status IN ('unique','resolved')
          )
        """,
        [],
    )


def _relationship_players(where: str, params: list) -> tuple[str, list]:
    # Return both ends of a trusted explicit relationship.
    sql = f"""
        SELECT DISTINCT a.player_id AS player_id
        FROM family_relationships r
        JOIN family_members a
          ON a.source_member_id=r.person_a_source_member_id
        JOIN family_members b
          ON b.source_member_id=r.person_b_source_member_id
        WHERE a.player_id IS NOT NULL
          AND b.player_id IS NOT NULL
          AND a.match_status IN ('unique','resolved')
          AND b.match_status IN ('unique','resolved')
          AND ({where})
        UNION
        SELECT DISTINCT b.player_id AS player_id
        FROM family_relationships r
        JOIN family_members a
          ON a.source_member_id=r.person_a_source_member_id
        JOIN family_members b
          ON b.source_member_id=r.person_b_source_member_id
        WHERE a.player_id IS NOT NULL
          AND b.player_id IS NOT NULL
          AND a.match_status IN ('unique','resolved')
          AND b.match_status IN ('unique','resolved')
          AND ({where})
    """
    return sql, list(params) + list(params)


def sibling_also_played():
    """Has an explicitly identified sibling in the AFL/VFL player table."""
    return _relationship_players("r.relationship_type='sibling'", [])


def brother_also_played():
    """Has an explicit brother/brothers relationship (twins included)."""
    return _relationship_players(
        "r.relationship_type='sibling' "
        "AND (LOWER(r.relationship_label) LIKE '%brother%' "
        "OR LOWER(r.relationship_label)='twins' "
        "OR LOWER(r.person_a_role)='brother' "
        "OR LOWER(r.person_b_role)='brother')",
        [],
    )


def parent_or_child_also_played():
    return _relationship_players("r.relationship_type='parent_child'", [])


def father_or_son_also_played():
    """Trusted parent/child link explicitly identifying a father or son."""
    return _relationship_players(
        "r.relationship_type='parent_child' "
        "AND (LOWER(r.relationship_label) LIKE '%father%' "
        "OR LOWER(r.relationship_label) LIKE '%son%' "
        "OR LOWER(r.person_a_role) IN ('father','son') "
        "OR LOWER(r.person_b_role) IN ('father','son'))",
        [],
    )


def extended_family_also_played():
    """Grandparent, aunt/uncle, niece/nephew, cousin or in-law link."""
    return _relationship_players(
        "r.relationship_type IN ("
        "'grandparent_grandchild','aunt_uncle_niece_nephew','cousin','in_law')",
        [],
    )


def same_listed_family_as(player_id):
    """Other AFL/VFL players in the same Wikipedia family section."""
    return (
        """
        SELECT DISTINCT relative.player_id
        FROM family_members target
        JOIN family_members relative
          ON relative.family_key=target.family_key
         AND relative.source_member_id<>target.source_member_id
        WHERE target.player_id=?
          AND target.match_status IN ('unique','resolved')
          AND relative.player_id IS NOT NULL
          AND relative.match_status IN ('unique','resolved')
          AND relative.player_id<>?
        """,
        [int(player_id), int(player_id)],
    )


def relative_played_for(club):
    """Has another linked family member who played for ``club``."""
    return (
        """
        SELECT DISTINCT self.player_id
        FROM family_members self
        JOIN family_members relative
          ON relative.family_key=self.family_key
         AND relative.source_member_id<>self.source_member_id
        JOIN games g ON g.player_id=relative.player_id
        WHERE self.player_id IS NOT NULL
          AND relative.player_id IS NOT NULL
          AND self.match_status IN ('unique','resolved')
          AND relative.match_status IN ('unique','resolved')
          AND self.player_id<>relative.player_id
          AND (LOWER(TRIM(g.club_now))=LOWER(TRIM(?))
               OR LOWER(TRIM(g.club_hist))=LOWER(TRIM(?)))
        """,
        [club, club],
    )


FAMILY_RELATIONSHIP_BUILDERS = {
    "AFL/VFL family member":            (family_member, []),
    "Sibling also played AFL/VFL":      (sibling_also_played, []),
    "Brother also played AFL/VFL":      (brother_also_played, []),
    "Parent/child also played AFL/VFL": (parent_or_child_also_played, []),
    "Father/son also played AFL/VFL":   (father_or_son_also_played, []),
    "Extended family also played AFL/VFL": (extended_family_also_played, []),
    "Same listed family as…":           (same_listed_family_as, ["player_id"]),
    "Relative played for club":         (relative_played_for, ["club"]),
}
