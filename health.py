#!/usr/bin/env python3
"""Database health reporting for Sports Data Lab.

One place to see what is loaded, how good the optional link layers are, and
where the data has known gaps -- instead of running several scripts and
reading their separate outputs.

Every check is read-only and degrades gracefully: a missing optional table
reports as "not loaded" rather than raising, so this page works on a clean
core build as well as a fully enriched one.

Usable two ways::

    python health.py --db gridley.db          # text report
    python health.py --db gridley.db --json   # machine-readable

and as the "Database Health" page inside app.py via :func:`health_page`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

#: Optional link layers: table -> (label, id column, script hint).
LINK_LAYERS = {
    "captaincies": ("Captaincy", "player_id", "afl/load_captains.py"),
    "rising_star_nominees": ("Rising Star", "player_id", "afl/load_rising_star.py"),
}
TRUSTED_STATUSES = ("unique", "resolved")


def _schema(schema=None):
    """The schema to check against, defaulting to the AFL build.

    Every core probe takes an optional `schema` so the same checks can run
    against an NBA database, where the post-season column is
    `playoffs_played` and the headline stat is `points`. Passing nothing
    keeps the AFL behaviour byte-identical, which is what the CLI's default
    and every existing caller rely on.
    """
    if schema is not None:
        return schema
    import sports
    return sports.AFL_SCHEMA


# ------------------------------------------------------------------ probes

def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def table_counts(con: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every table and its row count, largest first."""
    names = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    out = []
    for name in names:
        try:
            out.append((name, con.execute(
                f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0]))
        except sqlite3.Error:
            out.append((name, -1))
    return sorted(out, key=lambda row: -row[1])


def core_summary(con: sqlite3.Connection, schema=None) -> dict:
    out: dict = {}
    s = _schema(schema)
    if not table_exists(con, s.games) or not table_exists(con, s.players):
        return {"error": f"core tables missing; run {s.rebuild_cmd}"}
    lo, hi = con.execute(
        f"SELECT MIN({s.season}), MAX({s.season}) FROM {s.games}").fetchone()
    out["season_min"], out["season_max"] = lo, hi
    out["seasons"] = con.execute(
        f"SELECT COUNT(DISTINCT {s.season}) FROM {s.games}").fetchone()[0]
    out["players"] = con.execute(
        f"SELECT COUNT(*) FROM {s.players}").fetchone()[0]
    out["player_games"] = con.execute(
        f"SELECT COUNT(*) FROM {s.games}").fetchone()[0]
    if table_exists(con, "matches"):
        out["matches"] = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    if table_exists(con, "team_seasons"):
        out["team_seasons"] = con.execute(
            "SELECT COUNT(*) FROM team_seasons").fetchone()[0]
    return out


def link_quality(con: sqlite3.Connection) -> list[dict]:
    """Trusted / untrusted breakdown for each optional link layer."""
    report = []
    for table, (label, id_col, script) in LINK_LAYERS.items():
        entry = {"layer": label, "table": table, "script": script}
        if not table_exists(con, table):
            entry["state"] = "not loaded"
            report.append(entry)
            continue
        cols = columns(con, table)
        if "match_status" not in cols:
            entry["state"] = "unexpected schema"
            report.append(entry)
            continue
        statuses = dict(con.execute(
            f"SELECT match_status, COUNT(*) FROM {table} GROUP BY match_status"
        ).fetchall())
        total = sum(statuses.values())
        trusted = sum(statuses.get(s, 0) for s in TRUSTED_STATUSES)
        entry.update({
            "state": "loaded" if total else "empty",
            "total": total,
            "trusted": trusted,
            "untrusted": total - trusted,
            "statuses": statuses,
        })
        if "match_method" in cols:
            entry["methods"] = dict(con.execute(
                f"SELECT match_method, COUNT(*) FROM {table} "
                f"WHERE match_status IN {TRUSTED_STATUSES} GROUP BY match_method"
            ).fetchall())
        report.append(entry)
    return report


def untrusted_rows(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """The actual rows needing review, so problems are actionable."""
    out = []
    for table, (label, _id, _script) in LINK_LAYERS.items():
        if not table_exists(con, table):
            continue
        cols = columns(con, table)
        if "match_status" not in cols:
            continue
        name_col = "player" if "player" in cols else None
        season_col = "season" if "season" in cols else None
        club_col = "club" if "club" in cols else None
        select = ", ".join(c for c in (season_col, name_col, club_col) if c)
        if not select:
            continue
        extra = ", notes" if "notes" in cols else ""
        rows = con.execute(
            f"SELECT match_status, {select}{extra} FROM {table} "
            f"WHERE match_status NOT IN {TRUSTED_STATUSES} "
            f"ORDER BY {season_col or select} LIMIT ?", (limit,)
        ).fetchall()
        for row in rows:
            out.append({"layer": label, "status": row[0],
                        "detail": " | ".join(str(v) for v in row[1:] if v)})
    return out


def rising_star_coverage(con: sqlite3.Connection) -> dict:
    """Season coverage and the latest archived nomination season."""
    if not table_exists(con, "rising_star_nominees"):
        return {"state": "not loaded"}
    rows = con.execute(
        "SELECT season, COUNT(*) FROM rising_star_nominees "
        "GROUP BY season ORDER BY season"
    ).fetchall()
    if not rows:
        return {"state": "empty"}
    seasons = [season for season, _ in rows]
    missing = [s for s in range(min(seasons), max(seasons) + 1)
               if s not in set(seasons)]
    return {
        "state": "loaded",
        "season_min": min(seasons),
        "season_max": max(seasons),
        "seasons": len(seasons),
        "latest_count": rows[-1][1],
        "missing_seasons": missing,
        "by_season": rows,
    }


def stat_era_starts(con: sqlite3.Connection, stats: list[str] | None = None,
                    schema=None) -> list[tuple]:
    """First season each detailed statistic carries a non-null, non-zero value.

    Passing `schema` uses that sport's declared stat list, which is how the
    NBA gets its own era table without a second copy of this query. An
    explicit `stats` still wins, for callers auditing one column.
    """
    s = _schema(schema)
    if not table_exists(con, s.games):
        return []
    cols = columns(con, s.games)
    if stats is None and schema is not None and schema.stats:
        stats = [stat for stat in schema.stats if stat in cols]
    candidates = stats or [s for s in (
        "disposals", "kicks", "handballs", "marks", "tackles", "hitouts",
        "inside50s", "clearances", "rebounds", "contested", "contested_marks",
        "marks_i50", "one_percenters", "goal_assists", "brownlow", "goals",
        "behinds", "bounces",
        # Loaded by afl/build_db.py but absent from this list, so the health
        # report never showed whether they were populated at all.
        "frees_for", "frees_against", "clangers", "uncontested",
    ) if s in cols]
    out = []
    for stat in candidates:
        row = con.execute(
            f"SELECT MIN({s.season}) FROM {s.games} "
            f"WHERE {stat} IS NOT NULL AND {stat} != 0"
        ).fetchone()
        out.append((stat, row[0] if row else None))
    return sorted(out, key=lambda r: (r[1] is None, r[1] or 0))


#: What each table is for, so the inventory reads as an explanation of the
#: database rather than a list of names. Tables with no entry still appear;
#: an unexplained table is a prompt to document it, not something to hide.
TABLE_PURPOSE = {
    "players": "One row per person, with career totals and the obscurity score",
    "games": "One row per player per match — the core fact table",
    "matches": "One row per match, derived from games",
    "match_details": "Quarter scores and attendance per match, linked",
    "club_match_sources": "One row per club per match from the all-games scrape",
    "club_match_source_issues": "Recorded disagreements between the two sides",
    "team_seasons": "Season record and final standing per club",
    "season_goals": "Per-club leading goalkicker by season",
    # NBA (nba/build_nba_db.py). The engine reads players/games as above; these
    # are the normalised source of truth the text columns are derived from.
    "franchises": "One row per continuous organisation, across relocations",
    "teams": "One row per team identity — the club_hist values",
    "team_aliases": "Alternative spellings and abbreviations per team",
    "player_seasons": "Per-player per-season totals, split by phase",
    "player_team_history": "Which teams a player appeared for, and when",
    "source_manifest": "Every source retrieval: what, when, and its digest",
    "source_issues": "Anything the build reconciled rather than trusted",
    "clubs": "Current-club catalogue from the club-sources scrape",
    "club_wikipedia_fields": "Scraped infobox fields per club",
    "club_source_snapshots": "When each club source page was fetched",
    "club_player_totals": "Career totals per player per club",
    "club_player_register": "All-time player list per club",
    "club_player_records": "Season and game record leaderboards per club",
    "club_player_averages": "Per-season averages per player per club",
    "draft": "Draft and signing rows from Draftguru",
    "draft_links": "Draft rows resolved to a player_id",
    "dg_people": "Draftguru person records",
    "person_links": "Draftguru people resolved to a player_id",
    "awards": "Award winners and placings",
    "all_australian": "All-Australian selections",
    "captaincies": "Club captains by season",
    "rising_star_nominees": "FootyWire Rising Star nominations",
    "family_members": "People in a listed football family",
    "family_relationships": "Explicit relationships between people",
    "family_draft": "Father-son and academy draft rows",
    "hall_of_fame": "Australian Football Hall of Fame inductees",
    "team_selections": "Team of the Century selections",
    "stat_coverage": "Measured era each statistic actually covers",
    "meta": "Build timestamps and source URLs",
}


def stat_coverage_rows(con: sqlite3.Connection) -> list[dict]:
    """The measured coverage table, if load_stat_coverage.py has run."""
    if not table_exists(con, "stat_coverage"):
        return []
    return [
        {"Statistic": stat, "From": lo, "To": hi, "Notes": notes}
        for stat, lo, hi, notes in con.execute(
            "SELECT stat_name, available_from, available_to, coverage_notes "
            "FROM stat_coverage ORDER BY available_from, stat_name")
    ]


def match_coverage(con: sqlite3.Connection) -> dict:
    """What the all-games layer holds, and how complete it is."""
    if not table_exists(con, "club_match_sources"):
        return {"state": "not loaded"}
    total, matches, lo, hi, crowds, finals = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT source_game_key), "
        "       MIN(season), MAX(season), SUM(attendance IS NOT NULL), "
        "       SUM(is_final) FROM club_match_sources").fetchone()
    statuses = dict(con.execute(
        "SELECT match_status, COUNT(*) FROM club_match_sources "
        "GROUP BY match_status"))
    return {
        "state": "loaded",
        "observations": total,
        "matches": matches,
        "season_min": lo,
        "season_max": hi,
        "with_attendance": crowds or 0,
        "finals": finals or 0,
        "clubs": con.execute(
            "SELECT COUNT(DISTINCT source_club_id) "
            "FROM club_match_sources").fetchone()[0],
        "venues": con.execute(
            "SELECT COUNT(DISTINCT venue_raw) "
            "FROM club_match_sources").fetchone()[0],
        "statuses": statuses,
    }


def inventory(con: sqlite3.Connection, schema=None) -> dict:
    """Counts of the things a user would think of as "what's in here"."""
    out: dict = {}
    s = _schema(schema)
    if table_exists(con, s.games):
        out["clubs"] = con.execute(
            f"SELECT COUNT(DISTINCT {s.club_now}) FROM {s.games}").fetchone()[0]
        out["club_identities"] = con.execute(
            f"SELECT COUNT(*) FROM (SELECT {s.club_now} AS c FROM {s.games} "
            f"UNION SELECT {s.club_hist} FROM {s.games})").fetchone()[0]
        out["venues"] = con.execute(
            f"SELECT COUNT(DISTINCT {s.venue}) FROM {s.games}").fetchone()[0]
        out["seasons"] = con.execute(
            f"SELECT COUNT(DISTINCT {s.season}) FROM {s.games}").fetchone()[0]
        out["finals"] = con.execute(
            f"SELECT COUNT(*) FROM {s.games} "
            f"WHERE {s.is_final} = 1").fetchone()[0]
    if table_exists(con, s.players):
        out["one_game_players"] = con.execute(
            f"SELECT COUNT(*) FROM {s.players} "
            f"WHERE {s.career_games} = 1").fetchone()[0]
        out["still_playing"] = con.execute(
            f"SELECT COUNT(*) FROM {s.players} WHERE {s.final_season} = "
            f"(SELECT MAX({s.season}) FROM {s.games})").fetchone()[0]
    return out


def per_season_rows(con: sqlite3.Connection, schema=None) -> list[tuple]:
    """Player-games and distinct players per season, for a coverage chart."""
    s = _schema(schema)
    if not table_exists(con, s.games):
        return []
    return con.execute(
        f"SELECT {s.season}, COUNT(*), COUNT(DISTINCT {s.player_id}) "
        f"FROM {s.games} GROUP BY {s.season} ORDER BY {s.season}").fetchall()


def database_file(con: sqlite3.Connection) -> dict:
    """Size on disk and page statistics, so growth is visible."""
    try:
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
    except sqlite3.Error:
        return {}
    return {
        "bytes": page_size * page_count,
        "pages": page_count,
        "free_pages": freelist,
        "indexes": con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
        ).fetchone()[0],
    }


def career_totals_reconcile(con: sqlite3.Connection, schema=None) -> list[str]:
    """
    The players table must agree with the games it was aggregated from.

    Every career column is a groupby over `games`, so a duplicate row that
    survived deduplication, a dropped row, or a rescore against a stale
    frame shows up here and nowhere else -- the number still looks
    plausible, it is just wrong. That is the failure mode worth failing a
    build over, because a wrong career_games silently moves an obscurity
    score and every star rating derived from it.

    Sums are compared only where the games-side sum is non-NULL, so a stat
    that predates its recording era is not reported as a disagreement.
    """
    s = _schema(schema)
    out: list[str] = []
    if not (table_exists(con, s.games) and table_exists(con, s.players)):
        return out
    player_cols = columns(con, s.players)

    # The default reads one games row as one game and sums every row, which
    # is what the AFL and NBA builds write. A sport whose games table has a
    # different grain says so in schema.career_totals_sql -- see the MLB
    # entry in sports.py, where a row is a season.
    checks = [
        (s.career_games, "COUNT(*)", "career games"),
        (s.career_score, f"SUM(g.{s.game_score})", "career " + s.game_score),
        (s.career_postseason, f"SUM(g.{s.is_final})", "post-season games"),
    ]
    for column, default, label in checks:
        if column not in player_cols:
            continue
        override = s.career_totals_sql.get(column, (default, ""))
        if override is None:
            continue
        expression, predicate = override
        where = f"WHERE {predicate}" if predicate else ""
        try:
            bad = con.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT p.{s.player_id}
                    FROM {s.players} p
                    JOIN {s.games} g ON g.{s.player_id} = p.{s.player_id}
                    {where}
                    GROUP BY p.{s.player_id}, p.{column}
                    HAVING {expression} IS NOT NULL
                       AND p.{column} IS NOT NULL
                       AND p.{column} != {expression})
            """).fetchone()[0]
        except sqlite3.Error:
            continue
        if bad:
            out.append(f"{bad:,} player(s): stored {label} disagrees with "
                       f"the {s.games} rows it was aggregated from")
    return out


def integrity_warnings(con: sqlite3.Connection, schema=None) -> list[str]:
    """Cheap checks that catch the failure modes seen in this project."""
    warnings = []
    s = _schema(schema)
    if not table_exists(con, s.games):
        return [f"core {s.games} table missing"]

    game_cols = columns(con, s.games)
    # match_id first: it identifies the actual fixture, which is what a
    # duplicate player-game means. Dates alone cannot separate the two legs
    # of an NBA doubleheader, and the AFL build has no match_id on `games`
    # until derive_matches has run, so both shapes have to work.
    key_cols = [c for c in ("player_id", "match_id", "season", "round", "date",
                            "club_hist") if c in game_cols]
    # Without a match, round or date column, several games in one season at one
    # club are indistinguishable and every multi-game season looks duplicated.
    if {"match_id", "round", "date"} & set(key_cols) and len(key_cols) >= 3:
        keys = ", ".join(key_cols)
        dupes = con.execute(
            f"SELECT COUNT(*) FROM (SELECT {keys} FROM {s.games} "
            f"GROUP BY {keys} HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if dupes:
            warnings.append(f"{dupes:,} duplicate player-game keys")

    orphans = con.execute(
        f"SELECT COUNT(*) FROM {s.games} g LEFT JOIN {s.players} p "
        f"ON p.{s.player_id} = g.{s.player_id} "
        f"WHERE p.{s.player_id} IS NULL"
    ).fetchone()[0]
    if orphans:
        warnings.append(f"{orphans:,} game rows have no matching player")

    warnings.extend(career_totals_reconcile(con, s))

    # Wooden-spoon integrity: the bug that needed repair_database.py.
    if table_exists(con, "team_seasons") and "ladder_pos" in columns(con, "team_seasons"):
        bad = con.execute(
            "SELECT COUNT(*) FROM (SELECT season FROM team_seasons "
            "WHERE ladder_pos IS NOT NULL GROUP BY season "
            "HAVING COUNT(*) FILTER (WHERE ladder_pos = ("
            "  SELECT MAX(ladder_pos) FROM team_seasons t2 "
            "  WHERE t2.season = team_seasons.season)) != 1)"
        ).fetchone()[0]
        if bad:
            warnings.append(f"{bad} season(s) lack exactly one last-placed club "
                            "(run repair_database.py)")

    # Unrendered source templates that silently break club linking. Rows
    # already corrected by a reviewed override are not a problem: the source
    # wording is deliberately retained, so only untrusted leaks are reported.
    if table_exists(con, "rising_star_nominees"):
        leaks = con.execute(
            "SELECT COUNT(*) FROM rising_star_nominees "
            "WHERE (club LIKE '%$%' OR club LIKE '%{%') "
            f"AND match_status NOT IN {TRUSTED_STATUSES}"
        ).fetchone()[0]
        if leaks:
            warnings.append(f"{leaks} Rising Star row(s) contain unrendered "
                            "source template text and are unresolved")

    for table, (label, id_col, script) in LINK_LAYERS.items():
        if table_exists(con, table) and "match_status" in columns(con, table):
            untrusted = con.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE match_status NOT IN {TRUSTED_STATUSES}"
            ).fetchone()[0]
            if untrusted:
                warnings.append(f"{label}: {untrusted} row(s) not trusted "
                                "by search/solver")
    return warnings


def source_dates(con: sqlite3.Connection) -> list[tuple[str, str]]:
    if not table_exists(con, "meta"):
        return []
    try:
        return con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    except sqlite3.Error:
        return []


def collect(con: sqlite3.Connection, schema=None) -> dict:
    """Everything, in one dict, for the UI and the CLI to share.

    `schema` threads through the core probes so the same report works for a
    second sport. The optional-layer probes below it are all AFL-shaped but
    every one of them is table-gated, so on an NBA database they report
    "not loaded", which is the honest answer rather than an error.
    """
    return {
        "core": core_summary(con, schema),
        "tables": table_counts(con),
        "links": link_quality(con),
        "untrusted": untrusted_rows(con),
        "rising_star": rising_star_coverage(con),
        "stat_eras": stat_era_starts(con, schema=schema),
        "stat_coverage": stat_coverage_rows(con),
        "match_coverage": match_coverage(con),
        "inventory": inventory(con, schema),
        "per_season": per_season_rows(con, schema),
        "file": database_file(con),
        "warnings": integrity_warnings(con, schema),
        "meta": source_dates(con),
    }


# ------------------------------------------------------------------ report

def print_report(report: dict) -> None:
    core = report["core"]
    print("=" * 62)
    print("DATABASE HEALTH")
    print("=" * 62)
    if "error" in core:
        print(f"  {core['error']}")
        return
    print(f"  Seasons        {core['season_min']}-{core['season_max']} "
          f"({core['seasons']} seasons)")
    print(f"  Players        {core['players']:,}")
    print(f"  Player-games   {core['player_games']:,}")
    for key in ("matches", "team_seasons"):
        if key in core:
            print(f"  {key:<14} {core[key]:,}")

    print("\nOPTIONAL LAYERS")
    for entry in report["links"]:
        if entry["state"] != "loaded":
            print(f"  {entry['layer']:<14} {entry['state']} "
                  f"(run {entry['script']})")
            continue
        print(f"  {entry['layer']:<14} {entry['trusted']:,} trusted "
              f"/ {entry['total']:,} total"
              + (f"  [{entry['untrusted']} NOT trusted]"
                 if entry["untrusted"] else ""))
        for status, count in sorted(entry.get("statuses", {}).items()):
            print(f"      {status:<16} {count:>6,}")

    rs = report["rising_star"]
    if rs.get("state") == "loaded":
        print("\nRISING STAR COVERAGE")
        print(f"  Seasons        {rs['season_min']}-{rs['season_max']} "
              f"({rs['seasons']} archived)")
        print(f"  Latest season  {rs['season_max']} "
              f"({rs['latest_count']} nominations)")
        if rs["missing_seasons"]:
            print(f"  Missing        {rs['missing_seasons']}")

    if report["untrusted"]:
        print("\nROWS NEEDING REVIEW")
        for row in report["untrusted"]:
            print(f"  [{row['layer']}/{row['status']}] {row['detail']}")

    if report["stat_eras"]:
        print("\nDETAILED-STAT START SEASONS")
        for stat, season in report["stat_eras"]:
            print(f"  {stat:<18} {season if season else 'never recorded'}")

    if report["meta"]:
        print("\nSOURCE REFRESH")
        for key, value in report["meta"]:
            print(f"  {key:<24} {value}")

    print("\nWARNINGS")
    if report["warnings"]:
        for warning in report["warnings"]:
            print(f"  ! {warning}")
    else:
        print("  none -- all checks clean")
    print("=" * 62)


# ------------------------------------------------------------------ UI page

def health_page(SPORT, con) -> None:
    """Streamlit page. Imported lazily so the CLI needs no Streamlit."""
    import pandas as pd
    import streamlit as st

    st.header("Database Health")
    st.caption("Read-only diagnostics: what is loaded, how good the optional "
               "link layers are, and where the data has known gaps.")
    report = collect(con, SPORT.schema)
    core = report["core"]
    if "error" in core:
        st.error(core["error"])
        return

    inv = report["inventory"]
    fileinfo = report["file"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Seasons", f"{core['season_min']}–{core['season_max']}",
              help=f"{core['seasons']} seasons with at least one match")
    c2.metric("Players", f"{core['players']:,}",
              help=f"{inv.get('one_game_players', 0):,} played exactly one "
                   f"{SPORT.vocab.game}")
    c3.metric(f"Player-{SPORT.vocab.games}", f"{core['player_games']:,}")
    c4.metric("Matches", f"{core.get('matches', 0):,}",
              help=f"{inv.get('finals', 0):,} finals player-{SPORT.vocab.games}")

    d1, d2, d3, d4 = st.columns(4)
    d1.metric(SPORT.vocab.title_case("clubs"), f"{inv.get('clubs', 0)}",
              help=f"{inv.get('club_identities', 0)} identities including "
                   "historical names")
    d2.metric(SPORT.vocab.title_case("venues"), f"{inv.get('venues', 0)}")
    d3.metric("Still playing", f"{inv.get('still_playing', 0):,}",
              help=f"final season is {core['season_max']}")
    d4.metric("Database size",
              f"{fileinfo.get('bytes', 0) / 1_048_576:.0f} MB",
              help=f"{fileinfo.get('indexes', 0)} indexes, "
                   f"{fileinfo.get('free_pages', 0):,} free pages")

    warnings = report["warnings"]
    if warnings:
        for warning in warnings:
            st.warning(warning)
    else:
        st.success("All integrity checks clean.")

    st.subheader("Optional layers")
    rows = []
    for entry in report["links"]:
        rows.append({
            "Layer": entry["layer"],
            "State": entry["state"],
            "Trusted": f"{entry.get('trusted', 0):,}",
            "Not trusted": entry.get("untrusted", 0),
            "Total": f"{entry.get('total', 0):,}",
            "Loader": entry["script"],
        })
    st.dataframe(rows, width="stretch", hide_index=True)

    if report["untrusted"]:
        st.subheader("Rows needing review")
        st.caption("Retained for audit; excluded from search and solver results.")
        st.dataframe(report["untrusted"], width="stretch", hide_index=True)

    rs = report["rising_star"]
    if rs.get("state") == "loaded":
        st.subheader("Rising Star coverage")
        st.write(f"Archived {rs['season_min']}–{rs['season_max']} "
                 f"({rs['seasons']} seasons). Latest: {rs['season_max']} "
                 f"with {rs['latest_count']} nominations.")
        if rs["missing_seasons"]:
            st.warning(f"Missing seasons: {rs['missing_seasons']}")
        st.bar_chart({str(season): count for season, count in rs["by_season"]})

    mc = report["match_coverage"]
    if mc.get("state") == "loaded":
        st.subheader("Match data")
        st.caption(
            "The all-games layer: one row per club per match, which is what "
            "Past Games, club history and the crowd constraints read.")
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Matches", f"{mc['matches']:,}",
                  help=f"{mc['observations']:,} club-match observations")
        e2.metric("Seasons", f"{mc['season_min']}–{mc['season_max']}")
        pct = (100 * mc["with_attendance"] / mc["observations"]
               if mc["observations"] else 0)
        e3.metric("With attendance", f"{pct:.0f}%",
                  help=f"{mc['with_attendance']:,} of {mc['observations']:,}; "
                       "the rest are unrecorded at source, not a link failure")
        not_unique = sum(n for s, n in mc["statuses"].items() if s != "unique")
        e4.metric("Not cleanly linked", f"{mc['statuses'] and not_unique:,}",
                  help="Excluded from club history and crowd constraints")
        st.caption(
            f"{mc['clubs']} clubs · {mc['venues']} grounds · "
            f"{mc['finals']:,} finals observations · link outcomes: "
            + ", ".join(f"{n:,} {s}" for s, n in mc["statuses"].items()))

    st.subheader("What is in the database")
    st.caption("Every table, what it holds and how many rows.")
    st.dataframe(
        [{"Table": name, "Rows": f"{n:,}" if n >= 0 else "unreadable",
          "Holds": TABLE_PURPOSE.get(name, "—")}
         for name, n in report["tables"]],
        width="stretch", hide_index=True)

    coverage_rows = report["stat_coverage"]
    with st.expander("Statistic coverage by era"):
        st.caption(
            "Measured from the built database, not assumed. A statistic is "
            "empty before its first season, so a square using one cannot be "
            "satisfied by an earlier player — that is a gap in the record, "
            "not in the player.")
        if coverage_rows:
            st.dataframe(coverage_rows, width="stretch", hide_index=True)
        else:
            st.info("Run `python load_stat_coverage.py` for measured "
                    "coverage with population percentages.")
            st.dataframe(
                [{"Statistic": stat, "First recorded": season or "never"}
                 for stat, season in report["stat_eras"]],
                width="stretch", hide_index=True)

    per_season = report["per_season"]
    if per_season:
        with st.expander(f"Coverage by {SPORT.vocab.season}"):
            st.caption(
                f"Player-{SPORT.vocab.games} and distinct players per "
                f"{SPORT.vocab.season}. A dip is a shorter season — the war "
                "years and 2020 — not missing data.")
            st.line_chart(
                pd.DataFrame(
                    per_season,
                    columns=[SPORT.vocab.title_case("season"),
                             f"Player-{SPORT.vocab.games}", "Players"]
                ).set_index(SPORT.vocab.title_case("season")))

    with st.expander("Tables and row counts"):
        st.dataframe([{"Table": name, "Rows": f"{count:,}"}
                      for name, count in report["tables"]],
                     width="stretch", hide_index=True)

    if report["meta"]:
        with st.expander("Source refresh dates"):
            st.dataframe([{"Key": k, "Value": v} for k, v in report["meta"]],
                         width="stretch", hide_index=True)


# ------------------------------------------------------------------ CLI

def default_db(sport_key: str = "afl") -> str:
    try:
        from data_paths import sport_db
    except ImportError:      # data_paths sits beside this file; near-dead path
        from pathlib import Path
        return str(Path(__file__).resolve().parent / "gridley.db")
    return sport_db(sport_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sport", default="afl",
                        help="which sport's schema and database to check")
    parser.add_argument("--db", default=None,
                        help="database file; defaults to the sport's own")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when any warning is raised")
    args = parser.parse_args(argv)

    import sports
    sport = sports.get(args.sport)
    db = args.db or default_db(sport.key)

    if not Path(db).exists():
        print(f"No database at {db}. Run `{sport.build_cmd}` first.",
              file=sys.stderr)
        return 2
    con = sqlite3.connect(db)
    try:
        report = collect(con, sport.schema)
    finally:
        con.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    return 1 if (args.strict and report["warnings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
