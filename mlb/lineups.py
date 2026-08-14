"""Who took the field, for a database whose games rows are seasons.

Lahman's finest grain is a player-season, so an MLB match card has no box
score to draw and used to say only that. Retrosheet's game logs do record
both sides' batting orders, and `utils/mlb/load_retrosheet.py` stores them
one row per side, so this reads them back.

It is not a box score and does not pretend to be one: there are no hits or
at-bats here, only who started, where they batted and what they fielded.
That is the whole of what a game log carries, and it covers 235,472 of the
235,607 games ever played -- every one from 1901 on.

The position codes are Retrosheet's and are decoded here rather than in
the page, because a page shared by four sports has no business knowing
that 6 means shortstop.
"""

from __future__ import annotations

import re
import sqlite3

import pandas as pd

LINEUP_TABLE = "mlb_game_lineups"
RETRO_PLAYER_TABLE = "mlb_retro_players"

LINEUP_SEPARATOR = ";"
LINEUP_FIELD = ":"

#: Retrosheet's fielding position codes.
POSITIONS = {
    "1": "P", "2": "C", "3": "1B", "4": "2B", "5": "3B",
    "6": "SS", "7": "LF", "8": "CF", "9": "RF", "10": "DH",
}

#: What the card calls the column, and what a reader calls the thing.
HEADING = "Starting lineup"


#: A Retrosheet game key, as `utils/mlb/load_retrosheet.py` builds it:
#: date, game number, home code, visitor code.
_GAME_KEY = re.compile(r"^(\d{8})-(\d+)-([A-Z0-9]{3})-([A-Z0-9]{3})$")


def game_links(source_game_key) -> list[tuple[str, str]]:
    """(label, url) for reading this game somewhere that has the detail.

    This database records who started and what the score was; it does not
    hold a batting line, a play log or a win-probability graph, and it is
    not going to -- the sites that do publish those do not permit
    automated collection, which is why they are linked rather than
    scraped.

    Baseball-Reference's box URL is built from the same three things the
    game key holds -- home code, date and game number -- so it lands on
    the exact game, second half of a doubleheader included.

    FanGraphs is given as the date's scoreboard rather than a deep link.
    Their per-game URL needs a team spelling that is theirs alone, and a
    link that lands on the wrong game is worse than one more click.
    """
    match = _GAME_KEY.match(str(source_game_key or ""))
    if not match:
        return []
    date, number, home, _visitor = match.groups()
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return [
        ("Box score · Baseball-Reference",
         f"https://www.baseball-reference.com/boxes/{home}/"
         f"{home}{date}{number}.shtml"),
        ("Play log and win probability · FanGraphs",
         f"https://www.fangraphs.com/scoreboard.aspx?date={iso}"),
    ]


def available(con: sqlite3.Connection) -> bool:
    """Whether the lineup tables have been loaded."""
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (LINEUP_TABLE,)).fetchone())


def read(con: sqlite3.Connection, source_game_key, team_position
         ) -> pd.DataFrame:
    """One side's batting order: Order, Player, Pos, and the player id.

    Empty for a game with no lineup recorded -- most of the 19th century --
    so a caller can fall back to saying nothing is stored rather than
    drawing an empty table.

    The pitcher a designated hitter batted for is stored last with no
    batting order, which is exactly how a box score prints him.
    """
    if not source_game_key or not team_position or not available(con):
        return pd.DataFrame()
    row = con.execute(
        f"SELECT lineup FROM {LINEUP_TABLE} "
        f"WHERE source_game_key = ? AND team_position = ?",
        (str(source_game_key), str(team_position))).fetchone()
    if not row or not row[0]:
        return pd.DataFrame()

    entries = []
    for part in str(row[0]).split(LINEUP_SEPARATOR):
        fields = part.split(LINEUP_FIELD)
        retro_id = fields[0] if fields else ""
        if retro_id:
            position = fields[1] if len(fields) > 1 else ""
            # Blank where the player did not bat: the pitcher a designated
            # hitter batted for. Read rather than counted from the entry's
            # place, so a lineup the source recorded short of nine cannot
            # hand him somebody else's slot.
            order = fields[2] if len(fields) > 2 else ""
            entries.append((retro_id, position, order))
    if not entries:
        return pd.DataFrame()

    marks = ",".join("?" for _ in entries)
    known = {
        retro_id: (player, player_id)
        for retro_id, player, player_id in con.execute(
            f"SELECT retro_id, player, player_id FROM {RETRO_PLAYER_TABLE} "
            f"WHERE retro_id IN ({marks})",
            [retro_id for retro_id, _, _ in entries])
    }

    rows = []
    for retro_id, position, order in entries:
        player, player_id = known.get(retro_id, (None, None))
        rows.append({
            "Order": order or None,
            "Player": player or retro_id,
            "Pos": POSITIONS.get(position, position or ""),
            "PlayerID": player_id,
        })
    frame = pd.DataFrame(rows)
    frame["Order"] = pd.to_numeric(frame["Order"], errors="coerce").astype(
        "Int64")
    return frame
