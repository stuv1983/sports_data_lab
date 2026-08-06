"""Shared writer for `club_wikipedia_fields`, the Team Explorer's field table.

afl/club_explorer.py reads one key/value table for a club's headline tiles
and its Overview list. The AFL fills it from a Wikipedia infobox scrape --
hence the name -- but nothing in the reader is Wikipedia-specific, and the
other three sports have perfectly good reference data of their own that was
simply never loaded, leaving their Team Explorers showing four dashes.

Each sport's loader knows where its data lives and what its columns mean.
What they share is the writing: the same table, the same "an empty cell is
not a fact" rule, and the same need to record which file a value came from
so the Sources tab can trace a wrong one. That is what lives here.

The field keys are not free-form. afl/club_fields.py looks the headline
tiles up under fixed names, so a loader that wants its value to appear in
one uses the key named in `HEADLINE_KEYS` below; anything else is still
written and still shows in the Overview list, just not in a tile.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

TABLE = "club_wikipedia_fields"

#: The keys afl/club_fields.py resolves its four headline tiles from. A
#: sport's own words are applied at display time -- the NFL's `ground` is
#: labelled "Home stadium" and its `premierships` "Super Bowls" -- so a
#: loader stores the shared key and passes its own label alongside.
HEADLINE_KEYS = ("nickname", "founded", "ground", "premierships")


def ensure_table(con: sqlite3.Connection) -> None:
    """Create the table if utils/derive_club_tables.py has not already."""
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            club_id     TEXT NOT NULL,
            field_key   TEXT NOT NULL,
            field_label TEXT,
            field_value TEXT,
            field_html  TEXT,
            revision_id TEXT,
            imported_at TEXT,
            PRIMARY KEY (club_id, field_key)
        )
    """)


def club_ids_by_name(con: sqlite3.Connection) -> dict[str, str]:
    """Club name -> the club_id the Team Explorer selects clubs by.

    Both the display name and `db_club_now` are accepted as keys, because a
    sport's reference file may name a club either way.
    """
    mapping: dict[str, str] = {}
    for club_id, name, db_name in con.execute(
            "SELECT club_id, name, db_club_now FROM clubs"):
        for key in (name, db_name):
            if key:
                mapping.setdefault(key, club_id)
    return mapping


def write_fields(con: sqlite3.Connection, rows, source_label: str) -> int:
    """Write (club_id, key, label, value) rows. Returns the number written.

    A blank value is skipped rather than stored: an empty cell is not a
    fact, and writing it puts an empty row in the Overview list that reads
    as "we know this club has no nickname" rather than "we were not told".
    """
    ensure_table(con)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    written = 0
    for club_id, key, label, value in rows:
        text = "" if value is None else str(value).strip()
        if not club_id or not key or not text:
            continue
        con.execute(
            f"INSERT OR REPLACE INTO {TABLE} "
            f"(club_id, field_key, field_label, field_value, field_html, "
            f" revision_id, imported_at) VALUES (?,?,?,?,NULL,?,?)",
            (club_id, key, label, text, source_label, stamp))
        written += 1
    con.commit()
    return written


def summarise(con: sqlite3.Connection) -> tuple[int, int]:
    """(clubs with any field, total fields) -- what a loader prints."""
    return con.execute(
        f"SELECT COUNT(DISTINCT club_id), COUNT(*) FROM {TABLE}").fetchone()
