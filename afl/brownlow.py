"""Constraints over the optional AFL Tables Brownlow voting layer."""


TRUSTED = "match_status IN ('unique','resolved') AND player_id IS NOT NULL"

_PLACEHOLDER_SQL = """
CREATE TEMP TABLE IF NOT EXISTS brownlow_results (
    player_id INTEGER, season INTEGER, votes INTEGER, vote_rank INTEGER,
    eligible_rank INTEGER, ineligible INTEGER, winner INTEGER,
    match_status TEXT
)
"""


def _main_table_exists(con) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='brownlow_results'"
    ).fetchone())


def ensure_brownlow_table(con) -> None:
    if not _main_table_exists(con):
        con.execute(_PLACEHOLDER_SQL)


def brownlow_available(con) -> bool:
    if not _main_table_exists(con):
        return False
    return bool(con.execute(
        f"SELECT 1 FROM main.brownlow_results WHERE {TRUSTED} LIMIT 1"
    ).fetchone())


def brownlow_count(con) -> int:
    if not _main_table_exists(con):
        return 0
    return con.execute(
        f"SELECT COUNT(*) FROM main.brownlow_results WHERE {TRUSTED}"
    ).fetchone()[0]


def _positive(value, label: str) -> int:
    value = int(value)
    if value < 1:
        raise ValueError(f"{label} must be at least 1")
    return value


def brownlow_top_finish(place=5):
    place = _positive(place, "place")
    return (f"""SELECT DISTINCT player_id FROM brownlow_results
                 WHERE {TRUSTED} AND eligible_rank <= ?""", [place])


def brownlow_exact_finish(place=2):
    place = _positive(place, "place")
    return (f"""SELECT DISTINCT player_id FROM brownlow_results
                 WHERE {TRUSTED} AND eligible_rank = ? AND winner = 0""", [place])


def brownlow_top_finish_times(place=5, times=2):
    place = _positive(place, "place")
    times = _positive(times, "times")
    return (f"""SELECT player_id FROM brownlow_results
                 WHERE {TRUSTED} AND eligible_rank <= ?
                 GROUP BY player_id HAVING COUNT(DISTINCT season) >= ?""",
            [place, times])


def brownlow_votes_in_season(votes=20):
    votes = _positive(votes, "votes")
    return (f"""SELECT DISTINCT player_id FROM brownlow_results
                 WHERE {TRUSTED} AND votes >= ?""", [votes])


BROWNLOW_BUILDERS = {
    "Top X Brownlow finish": (brownlow_top_finish, ["place"]),
    "Exact Brownlow finish": (brownlow_exact_finish, ["place"]),
    "Top X Brownlow finish X+ times": (
        brownlow_top_finish_times, ["place", "times"]),
    "X+ Brownlow votes in a season": (brownlow_votes_in_season, ["votes"]),
}
