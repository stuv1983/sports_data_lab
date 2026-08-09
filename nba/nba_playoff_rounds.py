#!/usr/bin/env python3
"""
nba/nba_playoff_rounds.py -- Assign a playoff round to every post-season match.

The box-score sources say a game was a playoff game. They do not say which
round, and four criteria cannot answer anything without that: Finals
appearance, Finals win, appeared for the championship team, and the
championship derivation itself. `nba/build_nba_db.py` recorded the gap honestly
as a `no_finals_round` issue and refused to guess; this is what fills it.

HOW A GAME FINDS ITS SERIES
---------------------------
Within one season a playoff series is uniquely identified by the two teams
in it -- a bracket cannot pair the same two teams twice, and the reference
loader checks that it never does. So the join is (season, {team, team}),
which needs nothing from the source but what it already has, and in
particular does not need a series or game identifier that historical
sources mostly do not carry.

Team names are matched on the *historical* identity first. The reference
names a team as it was known in that season -- "Seattle SuperSonics", not
"Oklahoma City Thunder" -- so matching on the modern franchise name would
fail for every relocated team and, worse, could match the wrong one where a
name has been reused.

WHAT IT REFUSES TO DO
---------------------
A game whose team pair is not in the reference gets no round, an issue, and
a failed strict build. Filing it under a guess would put a first-round game
in the Finals or leave a title unassigned, and both are the kind of wrong
answer that looks perfectly reasonable on screen.
"""

import csv
from pathlib import Path

#: Leagues whose championships are NBA championships. The BAA is the NBA's
#: own predecessor -- the league counts its three titles and so do we. The
#: ABA was a separate league that merged in; its nine titles are not NBA
#: titles, and folding them in would make "champion" wrong for nine seasons
#: in a way nothing downstream could detect.
NBA_LINEAGE = ("NBA", "BAA")

REFERENCE = Path("reference") / "playoff_series.csv"

#: The 1953-54 playoffs opened with the only round-robin stage in NBA
#: history. It was the division semifinal round, but it was not a series, so
#: the series extract correctly has no team-pair row for its eight games.
#: The NBA's own season review describes the top-three round robin feeding
#: two teams into each division final.
NON_SERIES_ROUNDS = {1953: "CSF"}


class Series:
    """One playoff series, as the reference file records it."""

    __slots__ = ("season", "league", "round", "name", "winner", "loser",
                 "wins_winner", "wins_loser")

    def __init__(self, row):
        self.season = int(row["season"])
        self.league = row["league"]
        self.round = row["round"] or None
        self.name = row["series_name"]
        self.winner = row["winner"]
        self.loser = row["loser"]
        self.wins_winner = _int(row["wins_winner"])
        self.wins_loser = _int(row["wins_loser"])

    @property
    def games(self):
        """Games the series went, where the reference records both scores."""
        if self.wins_winner is None or self.wins_loser is None:
            return None
        return self.wins_winner + self.wins_loser


def load(root, leagues=NBA_LINEAGE):
    """Read the reference file. Returns {(season, frozenset(teams)): Series}.

    Keyed on the team pair because that is what a match can be looked up by.
    A season that ran two leagues keeps them apart by team: no franchise
    played in both in the same season.
    """
    path = Path(root) / REFERENCE
    if not path.exists():
        return {}
    index = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if leagues and row["league"] not in leagues:
                continue
            series = Series(row)
            index[(series.season,
                   frozenset((series.winner, series.loser)))] = series
    return index


def assign(matches, index, issue, verbose_log=None):
    """Fill `round` on every playoff match in `matches`, in place.

    `matches` is the build's frame, already validated, carrying season,
    phase, and both historical and current team names. `issue` is
    build_nba_db.issue bound to the current issue list.

    Returns a dict of counts: assigned, already_set, unresolved.
    """
    playoff = matches["phase"] == "playoff"
    if not playoff.any():
        return {"assigned": 0, "already_set": 0, "unresolved": 0}

    # A source that supplied no rounds at all leaves the column all-NaN,
    # which pandas types as float64 and then refuses to hold 'F'.
    matches["round"] = matches["round"].astype("object")

    assigned = already = unresolved = 0
    unmatched = []
    played = {}
    for position in matches.index[playoff]:
        row = matches.loc[position]
        found = _lookup(row, index)
        if found is not None:
            played[id(found)] = played.get(id(found), 0) + 1
        existing = _text(row.get("round"))
        if existing:
            # The source knew. Reference data does not overrule a source
            # that carried the round itself -- it fills a gap, and silently
            # rewriting a value the source supplied would hide a real
            # disagreement between the two.
            series = found
            if series and series.round and series.round != existing.upper():
                issue("round_disagreement",
                      f"match {row['match_id']!r} is round {existing!r} in "
                      f"the source and {series.round!r} ({series.name}) in "
                      f"the playoff-series reference; the source was kept",
                      severity="warn", season=int(row["season"]))
            already += 1
            continue

        if found is None or found.round is None:
            exceptional_round = NON_SERIES_ROUNDS.get(int(row["season"]))
            if found is None and exceptional_round:
                matches.at[position, "round"] = exceptional_round
                assigned += 1
                continue
            unresolved += 1
            unmatched.append((int(row["season"]), row.get("home_hist"),
                              row.get("away_hist")))
            continue
        matches.at[position, "round"] = found.round
        assigned += 1

    # A series the database has more or fewer games of than the reference
    # records means games are missing or two series have been conflated.
    # Worth reporting rather than failing over: an incomplete recent season
    # is a legitimate reason to be short.
    for series in {id(s): s for s in index.values()}.values():
        count = played.get(id(series))
        if count and series.games is not None and count != series.games:
            issue("series_length_disagreement",
                  f"{series.season} {series.name} ({series.winner} v "
                  f"{series.loser}): the reference records {series.games} "
                  f"game(s), the database has {count}",
                  severity="warn", season=series.season)

    if unmatched:
        sample = "; ".join(f"{s}: {h} v {a}" for s, h, a in unmatched[:5])
        issue("unresolved_playoff_round",
              f"{len(unmatched):,} playoff match(es) have no series in the "
              f"playoff-series reference and were left without a round "
              f"({sample}); Finals and championship criteria cannot see them",
              severity="error")
    if verbose_log:
        verbose_log(f"  playoff rounds: {assigned:,} assigned, "
                    f"{already:,} already set, {unresolved:,} unresolved")
    return {"assigned": assigned, "already_set": already,
            "unresolved": unresolved}


def _lookup(row, index):
    """Find the series for a match, historical identity first."""
    season = int(row["season"])
    for home, away in ((row.get("home_hist"), row.get("away_hist")),
                       (row.get("home_team"), row.get("away_team"))):
        if not home or not away:
            continue
        series = index.get((season, frozenset((home, away))))
        if series is not None:
            return series

    # NBA.com's old game logs reuse today's franchise id, so the historical
    # column can still say "Golden State Warriors" for a 1947 Philadelphia
    # Warriors game. Expand only as a fallback after both exact identities
    # failed. That order preserves defunct/reused names such as the original
    # Baltimore Bullets, while allowing a source with franchise-level ids to
    # meet the historical series reference honestly.
    from . import nba_reference

    lineage = nba_reference.club_lineage()
    home = row.get("home_team")
    away = row.get("away_team")
    home_names = lineage.get(home, [home])
    away_names = lineage.get(away, [away])
    for home_name in home_names:
        for away_name in away_names:
            if not home_name or not away_name:
                continue
            series = index.get(
                (season, frozenset((home_name, away_name))))
            if series is not None:
                return series
    return None


def verify(con, issue):
    """Every season with playoffs must have a Finals, and exactly one winner.

    Checked against what was written rather than against what was intended.
    A season that reaches here without a Finals produces no champion, and
    the four championship criteria answer nobody -- which reads on screen as
    "no player has ever won a title" rather than as a missing round.
    """
    for season, finals in con.execute(
            "SELECT season, SUM(CASE WHEN UPPER(TRIM(round))='F' THEN 1 "
            "ELSE 0 END) FROM matches WHERE phase='playoff' "
            "GROUP BY season ORDER BY season"):
        if not finals:
            issue("season_without_finals",
                  f"{season} has playoff matches but no Finals game; no "
                  f"champion can be derived for it",
                  severity="error", season=season)

    for season, winners in con.execute(
            "SELECT season, COUNT(*) FROM team_seasons WHERE champion=1 "
            "GROUP BY season HAVING COUNT(*) <> 1"):
        issue("champion_count",
              f"{season} derived {winners} champions; exactly one is required",
              severity="error", season=season)


def _text(value):
    """None-preserving str.

    A blank cell arrives from pandas as float('nan'), and str(nan) is the
    perfectly truthy string 'nan' -- which made every match with no round
    look like one whose source had supplied it, so nothing was ever filled
    in and every fill was reported as a disagreement.
    """
    if value is None:
        return None
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
