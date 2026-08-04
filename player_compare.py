"""Side-by-side comparison of two players.

A career total means little on its own -- 300 games is a lot, 400 goals
depends entirely on the era and the position. Putting two careers next to
each other is how most questions about a player actually get asked.

Everything here is read-only and derives from `players` and `games`, so it
works on a bare core build with no optional layer loaded. Where an optional
layer *is* present (awards, All-Australian, captaincy) it adds a row rather
than being required.

Era honesty carries through. Two players from different decades cannot be
compared on a statistic that was not recorded for both of them, so the
per-statistic rows are annotated with the seasons each player actually has
data for, and a statistic neither player could have recorded is dropped
rather than shown as a pair of zeroes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    """One player's comparable career summary."""
    player_id: int
    player: str
    debut_season: int | None
    final_season: int | None
    career_games: int
    career_score: int
    finals: int
    clubs: str
    obscurity: float | None
    totals: dict = field(default_factory=dict)     # stat -> career total
    per_game: dict = field(default_factory=dict)   # stat -> career average
    covered: dict = field(default_factory=dict)    # stat -> (from, to) seasons
    best: dict = field(default_factory=dict)       # stat -> best single game
    honours: list = field(default_factory=list)    # (label, detail)

    @property
    def span(self) -> str:
        if self.debut_season is None:
            return "—"
        return f"{self.debut_season}–{self.final_season}"

    @property
    def seasons(self) -> int:
        if self.debut_season is None:
            return 0
        return self.final_season - self.debut_season + 1


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _honours(con: sqlite3.Connection, player_id: int) -> list[tuple[str, str]]:
    """Award and selection rows, from whichever optional layers are loaded.

    Each layer is probed separately and contributes nothing when absent, so
    a comparison never depends on an import having been run.
    """
    out: list[tuple[str, str]] = []

    if _table_exists(con, "awards") and _table_exists(con, "person_links"):
        rows = con.execute(
            """SELECT a.award_name, COUNT(*), GROUP_CONCAT(a.season)
               FROM awards a
               JOIN person_links l ON l.dg_person_id = a.dg_person_id
               WHERE l.player_id = ?
                 AND l.match_status IN ('from_draft','unique','resolved')
               GROUP BY a.award_name
               ORDER BY COUNT(*) DESC, a.award_name""", (player_id,)
        ).fetchall()
        for name, n, seasons in rows:
            years = ", ".join(sorted(str(s) for s in seasons.split(",")))
            out.append((name, f"{n}× ({years})" if n > 1 else years))

    if _table_exists(con, "all_australian") and _table_exists(con, "person_links"):
        row = con.execute(
            """SELECT COUNT(*), MIN(aa.season), MAX(aa.season),
                      SUM(aa.is_captain)
               FROM all_australian aa
               JOIN person_links l ON l.dg_person_id = aa.dg_person_id
               WHERE l.player_id = ?
                 AND l.match_status IN ('from_draft','unique','resolved')""",
            (player_id,)).fetchone()
        if row and row[0]:
            detail = f"{row[0]}× ({row[1]}–{row[2]})" if row[0] > 1 \
                else str(row[1])
            if row[3]:
                detail += f", captain {row[3]}×"
            out.append(("All-Australian squad", detail))

    if _table_exists(con, "captaincies"):
        rows = con.execute(
            "SELECT club, MIN(season), MAX(season), COUNT(DISTINCT season) "
            "FROM captaincies WHERE player_id = ? "
            "AND match_status IN ('unique','resolved') GROUP BY club",
            (player_id,)).fetchall()
        for club, lo, hi, n in rows:
            years = str(lo) if lo == hi else f"{lo}–{hi}"
            out.append(("Club captain", f"{club} {years} ({n} seasons)"))

    # Two shapes exist. The AFL table is a scraped inductee list that had to
    # be name-matched onto players, so it carries a match_status and a Legend
    # flag; the MLB one is Lahman's ballot history keyed on player_id, where
    # a row only counts if it was actually an induction.
    if _table_exists(con, "hall_of_fame"):
        have = _columns(con, "hall_of_fame")
        if {"inducted_year", "is_legend"} <= have:
            row = con.execute(
                "SELECT inducted_year, is_legend FROM hall_of_fame "
                "WHERE player_id = ? AND match_status IN ('unique','resolved')",
                (player_id,)).fetchone()
            if row:
                label = "Hall of Fame (Legend)" if row[1] else "Hall of Fame"
                out.append((label, str(row[0] or "")))
        elif {"inducted", "season"} <= have:
            row = con.execute(
                "SELECT MIN(season) FROM hall_of_fame "
                "WHERE player_id = ? AND inducted = 'Y'",
                (player_id,)).fetchone()
            if row and row[0]:
                out.append(("Hall of Fame", str(row[0])))

    if _table_exists(con, "team_selections"):
        rows = con.execute(
            "SELECT team_name, position FROM team_selections "
            "WHERE player_id = ? AND match_status IN ('unique','resolved') "
            "ORDER BY team_name", (player_id,)).fetchall()
        for team, position in rows:
            out.append((team, position or "selected"))

    return out


def profile(con: sqlite3.Connection, player_id: int, schema,
            stats: list[str] | None = None,
            with_honours: bool = True) -> Profile | None:
    """Everything the comparison shows for one player."""
    s = schema
    row = con.execute(
        f"SELECT {s.player}, {s.debut_season}, {s.final_season}, "
        f"{s.career_games}, {s.career_score}, {s.career_postseason}, "
        f"{s.clubs_hist}, {s.obscurity} "
        f"FROM {s.players} WHERE {s.player_id} = ?", (player_id,)).fetchone()
    if not row:
        return None

    stats = list(stats if stats is not None else s.stats)
    totals, per_game, covered, best = {}, {}, {}, {}
    if stats:
        # One pass over the player's games for every statistic. A column
        # the player has no value for stays absent rather than becoming a
        # zero, which is the difference between "never did it" and "it was
        # not being recorded".
        select = ", ".join(
            f"SUM({stat}), AVG({stat}), MAX({stat}), "
            f"MIN(CASE WHEN {stat} IS NOT NULL THEN {s.season} END), "
            f"MAX(CASE WHEN {stat} IS NOT NULL THEN {s.season} END)"
            for stat in stats)
        values = con.execute(
            f"SELECT {select} FROM {s.games} WHERE {s.player_id} = ?",
            (player_id,)).fetchone()
        for i, stat in enumerate(stats):
            total, avg, high, lo, hi = values[i * 5:i * 5 + 5]
            if total is None:
                continue
            totals[stat] = total
            per_game[stat] = avg
            best[stat] = high
            covered[stat] = (lo, hi)

    return Profile(
        player_id=player_id, player=row[0], debut_season=row[1],
        final_season=row[2], career_games=row[3] or 0,
        career_score=int(row[4] or 0), finals=row[5] or 0,
        clubs=(row[6] or "").replace("|", ", "), obscurity=row[7],
        totals=totals, per_game=per_game, covered=covered, best=best,
        honours=_honours(con, player_id) if with_honours else [],
    )


def comparable_stats(a: Profile, b: Profile) -> list[str]:
    """Statistics both players have at least one recorded value for.

    A statistic only one of them could record is not a comparison, it is a
    fact about the eras they played in, and belongs in `era_gap` below.
    """
    return [stat for stat in a.totals if stat in b.totals]


def era_gap(a: Profile, b: Profile, stats: list[str]) -> list[str]:
    """Statistics one player has and the other cannot, with the reason."""
    notes = []
    for stat in stats:
        in_a, in_b = stat in a.totals, stat in b.totals
        if in_a == in_b:
            continue
        have, lack = (a, b) if in_a else (b, a)
        notes.append(
            f"{stat.replace('_', ' ')}: recorded for {have.player} "
            f"({have.covered[stat][0]}–{have.covered[stat][1]}) but not for "
            f"{lack.player} ({lack.span}) — not comparable.")
    return notes


def overlap(con: sqlite3.Connection, a: Profile, b: Profile,
            schema) -> dict:
    """Did they ever play in the same match, and were they teammates?

    Answered from `games` alone by matching on match_id, so it needs no
    optional layer. Teammates share a club in that match; opponents do not.
    """
    s = schema
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({s.games})")}
    if "match_id" not in cols:
        return {}
    rows = con.execute(
        f"""SELECT ga.{s.club_hist} = gb.{s.club_hist} AS same_side,
                   COUNT(*), MIN(ga.{s.season}), MAX(ga.{s.season})
            FROM {s.games} ga JOIN {s.games} gb
              ON gb.match_id = ga.match_id
            WHERE ga.{s.player_id} = ? AND gb.{s.player_id} = ?
            GROUP BY same_side""", (a.player_id, b.player_id)).fetchall()
    out = {"together": 0, "against": 0}
    for same_side, n, lo, hi in rows:
        key = "together" if same_side else "against"
        out[key] = n
        out[f"{key}_from"], out[f"{key}_to"] = lo, hi
    return out


def head_to_head_rows(a: Profile, b: Profile, stats: list[str]) -> list[dict]:
    """One row per comparable statistic, with the leader marked."""
    rows = []
    for stat in stats:
        ta, tb = a.totals[stat], b.totals[stat]
        rows.append({
            "Statistic": stat.replace("_", " "),
            a.player: ta,
            b.player: tb,
            "Difference": ta - tb,
            "Leader": a.player if ta > tb else (b.player if tb > ta else "—"),
        })
    return rows
