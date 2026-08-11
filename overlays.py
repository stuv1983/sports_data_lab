"""
overlays.py -- Season and club overviews, for any sport.

A results table answers one question and immediately raises another. A row
naming a season leaves "so who won that year?"; a row naming a club leaves
"how did they actually go?". Both used to need a different page, a
different filter and a lost place in the table.

This module renders the two answers, so any page can open either over the
table the reader is already looking at. Everything here is built from the
Sport's schema and vocabulary -- the champion comes from `title_round`
where a title is decided in one match and from `team_seasons.champion`
where it is not, the headline statistic is whatever the sport calls its
score, and the award list adapts to whichever `awards` shape the database
has.

Nothing in here opens a dialog: Streamlit allows exactly one dialog per
script run, and these bodies are what the dialogs in components.py render.
Tables inside an overview are therefore plain, not clickable.
"""

from __future__ import annotations

import base64
import html
import mimetypes
import os
import pathlib
import re
import sqlite3
from collections.abc import Mapping

import pandas as pd
import streamlit as st

import labels

# --------------------------------------------------------------- caching


def _revision(db):
    """Cache key that changes when the database file is replaced."""
    try:
        stat = os.stat(db)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _columns(con, table) -> set:
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has_table(con, table) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
            "AND name = ?", (table,)).fetchone())
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------- logos

@st.cache_data(show_spinner=False)
def _logo_index(sport_key, revision, _con) -> dict:
    """Club display name -> logo file, for whatever logos this sport has.

    Keyed on the name that appears in `games.club_now`, because that is the
    only club identity a results row carries. `clubs.db_club_now` is the
    column the club tables were built to join on.
    """
    from afl import club_logos as CL

    if not _has_table(_con, "clubs"):
        return {}
    found = CL.resolve(CL.clubs_from_db(_con), CL.logo_dir(sport_key))
    if not found:
        return {}
    index = {}
    columns = _columns(_con, "clubs")
    name_columns = [c for c in ("db_club_now", "name", "abbreviation")
                    if c in columns]
    for club_id, path in found.items():
        index[str(club_id).lower()] = str(path)
        if not name_columns:
            continue
        row = _con.execute(
            f"SELECT {', '.join(name_columns)} FROM clubs WHERE club_id = ?",
            (club_id,)).fetchone()
        for value in row or ():
            if value:
                index.setdefault(str(value).lower(), str(path))

    # Era and defunct names, straight out of the games table: a 1954 card
    # says Footscray and a 1990 card says Brisbane Bears, names the clubs
    # table does not hold. Each maps to its own era badge where the folder
    # has one (Footscray's colours, the Bears), and otherwise to the badge
    # of the club it became -- Kangaroos to North Melbourne's, South
    # Melbourne to Sydney's. A defunct club with no file and no successor
    # (Fitzroy, University) simply stays absent.
    import sports as sports_registry

    schema = sports_registry.get(sport_key).schema
    hist = getattr(schema, "club_hist", "")
    now = getattr(schema, "club_now", "")
    games = getattr(schema, "games", "")
    pairs = []
    if hist and now and games and _has_table(_con, games):
        try:
            pairs = _con.execute(
                f"SELECT DISTINCT {hist}, {now} FROM {games}").fetchall()
        except sqlite3.Error:
            pairs = []
    unindexed = {str(name) for pair in pairs for name in pair
                 if name and str(name).lower() not in index}
    era = CL.era_logo_files(unindexed, CL.logo_dir(sport_key),
                            exclude=set(found.values()))
    for h, n in pairs:
        for name, became in ((n, None), (h, n)):
            if not name or str(name).lower() in index:
                continue
            path = era.get(str(name)) or (
                index.get(str(became).lower()) if became else None)
            if path:
                index[str(name).lower()] = str(path)
    return index


def logo_for(sport, con, club) -> str | None:
    """The logo file for a club named as a results row names it."""
    if not club:
        return None
    index = _logo_index(sport.key, _revision(sport.db), con)
    return index.get(str(club).strip().lower())


def logo_html(path, height=64, alt="") -> str:
    """Inline the image as a data URI.

    Streamlit serves static files only from a configured folder and does
    not render SVG through st.image, and most of these logos are SVG.
    """
    data = pathlib.Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/svg+xml"
    return (f"<img src='data:{mime};base64,"
            f"{base64.b64encode(data).decode('ascii')}' "
            f"alt='{html.escape(str(alt), quote=True)}' "
            f"title='{html.escape(str(alt), quote=True)}' "
            f"style='height:{height}px;width:auto;max-width:100%;'/>")


# ----------------------------------------------------------- data notes

def season_notes(sport, season, heading="About this season's data") -> None:
    """Show the caveats that apply to one season, if the sport has any.

    Silent for a sport with no notes module and for a season nothing
    applies to, so any card can call it without first asking whether there
    is anything to say.
    """
    notes = sport.notes()
    if notes is None or season is None:
        return
    applicable = notes.for_season(season)
    if not applicable:
        return
    with st.expander(f"{heading} ({len(applicable)})"):
        for note in applicable:
            st.markdown(f"**{note.topic}** — {note.text}")


def round_caveat(sport, season, round_value) -> None:
    """Name the AFL's own number for a round, where it differs from ours.

    The single most confusing thing in the database: from 2024 our Round 23
    is the AFL's Round 22. It is said on the round card and the match card
    because that is where somebody reads a round number and compares it
    with a fixture.
    """
    notes = sport.notes()
    if notes is None or season is None:
        return
    note = notes.round_numbering_note(season)
    if note is None:
        return
    official = notes.official_round(round_value, season)
    if official:
        st.caption(f":material/info: The AFL's own fixture calls this "
                   f"**{official}**. {note.text}")
    else:
        st.caption(f":material/info: {note.text}")


# ------------------------------------------------------------- champions

@st.cache_data(show_spinner=False)
def champion(sport_key, season, revision, _con):
    """Who won the title that season, or None when the data cannot say.

    Two sources, in order of directness. A sport that decides its title in
    one match declares `title_round`, and the winner of that match is the
    champion. The NBA does not -- its Finals is a series and its rows are
    per-game -- so it carries the answer on `team_seasons.champion`
    instead, which is where its build records the series result.
    """
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    if sport.title_round:
        try:
            row = _con.execute(
                f"SELECT {sc.club_hist}, COUNT(*) FROM {sc.games} "
                f"WHERE {sc.season} = ? AND UPPER(TRIM({sc.round})) = ? "
                f"AND {sc.result} = 'W' "
                f"GROUP BY {sc.club_hist} ORDER BY COUNT(*) DESC LIMIT 1",
                (season, sport.title_round.upper())).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            return row[0]

    if "champion" in _columns(_con, "team_seasons"):
        try:
            row = _con.execute(
                "SELECT club_now FROM team_seasons "
                "WHERE season = ? AND champion = 1 LIMIT 1",
                (season,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            return row[0]
    return None


@st.cache_data(show_spinner=False)
def _standings(sport_key, season, revision, _con) -> pd.DataFrame:
    """The season's ladder, from `team_seasons` where the sport has one.

    Every sport writes a different set of columns there -- the AFL a
    ladder rank and percentage, the NBA a win percentage and conference,
    the NFL only per-team statistics -- so the frame is built from the
    columns actually present rather than from a fixed list.
    """
    columns = _columns(_con, "team_seasons")
    if not columns or "season" not in columns:
        return pd.DataFrame()
    club_column = ("club_now" if "club_now" in columns
                   else "team" if "team" in columns else None)
    if club_column is None or "wins" not in columns:
        return pd.DataFrame()

    wanted = [(club_column, "Club"), ("played", "P"), ("wins", "W"),
              ("draws", "D"), ("losses", "L"), ("percentage", "%"),
              ("win_pct", "Win %"), ("premiership_points", "Pts"),
              ("ladder_rank", "Rank"), ("conference", "Conference"),
              ("conference_rank", "Conf rank")]
    select = ", ".join(f'{col} AS "{header}"'
                       for col, header in wanted if col in columns)
    order = ("ladder_rank" if "ladder_rank" in columns
             else "win_pct DESC" if "win_pct" in columns else "wins DESC")
    where = "season = ?"
    params = [season]
    # The NBA keeps regular season and playoffs in the same table.
    if "phase" in columns:
        where += " AND phase = 'regular'"
    try:
        return pd.read_sql_query(
            f"SELECT {select} FROM team_seasons WHERE {where} "
            f"ORDER BY {order}", _con, params=params)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _leaders(sport_key, season, club, revision, _con) -> pd.DataFrame:
    """The season's leading scorers, in the sport's own headline statistic."""
    import sports

    sport = sports.get(sport_key)
    sc, V = sport.schema, sport.vocab
    where = [f"g.{sc.season} = ?", f"g.{sc.game_score} IS NOT NULL"]
    params = [season]
    if club:
        where.append(f"(g.{sc.club_hist} = ? OR g.{sc.club_now} = ?)")
        params += [club, club]
    try:
        return pd.read_sql_query(
            f"SELECT g.{sc.player_id} AS PlayerID, "
            f"       g.{sc.player} AS Player, "
            f"       g.{sc.club_hist} AS \"{V.club.capitalize()}\", "
            f"       SUM(g.{sc.game_score}) AS \"{V.score.capitalize()}\" "
            f"FROM {sc.games} g WHERE {' AND '.join(where)} "
            f"GROUP BY g.{sc.player_id} "
            f"ORDER BY SUM(g.{sc.game_score}) DESC LIMIT 10",
            _con, params=tuple(params))
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _season_awards(sport_key, season, revision, _con) -> pd.DataFrame:
    """Award winners for one season, whichever `awards` shape this is.

    The AFL's table came from Draftguru and names the award and the club
    on every row; the MLB's came from Lahman and carries a Lahman player
    id that joins straight to `players`. Detecting the shape from the
    columns keeps this one function rather than one per sport.
    """
    columns = _columns(_con, "awards")
    if not columns or "season" not in columns:
        return pd.DataFrame()
    try:
        if "award_name" in columns:
            select = ['award_name AS Award', 'player AS Player']
            if "club" in columns:
                select.append("club AS Club")
            if ("dg_person_id" in columns
                    and _columns(_con, "person_links")):
                select.append(
                    "(SELECT player_id FROM person_links l "
                    "WHERE l.dg_person_id = awards.dg_person_id "
                    "AND l.match_status IN "
                    "('from_draft','unique','resolved') LIMIT 1) AS PlayerID"
                )
            return pd.read_sql_query(
                f"SELECT {', '.join(select)} FROM awards WHERE season = ? "
                "ORDER BY award_name, player", _con, params=(season,))
        if "award" in columns and "player_id" in columns:
            return pd.read_sql_query(
                "SELECT a.award AS Award, p.player AS Player, "
                "a.player_id AS PlayerID "
                "FROM awards a LEFT JOIN players p "
                "  ON p.player_id = a.player_id "
                "WHERE a.season = ? ORDER BY a.award, p.player",
                _con, params=(season,))
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()
    return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _season_totals(sport_key, season, club, revision, _con):
    """(players, clubs) with a game row in one season."""
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    where = [f"{sc.season} = ?"]
    params = [season]
    if club:
        where.append(f"({sc.club_hist} = ? OR {sc.club_now} = ?)")
        params += [club, club]
    try:
        return _con.execute(
            f"SELECT COUNT(DISTINCT {sc.player_id}), "
            f"       COUNT(DISTINCT {sc.club_hist}) "
            f"FROM {sc.games} WHERE {' AND '.join(where)}",
            tuple(params)).fetchone()
    except sqlite3.Error:
        return (0, 0)


@st.cache_data(show_spinner=False)
def _club_season_record(sport_key, season, club, revision, _con):
    """One club's W-D-L for a season, or None when nothing records it.

    Read from `team_seasons`, not from `games`: a `games` row is a
    *player's* game, so counting results there counts each match once per
    player who played in it. Where a sport has no such table -- the NFL's
    holds statistics and no results -- the honest answer is no tile at all.
    """
    columns = _columns(_con, "team_seasons")
    if not {"season", "club_now", "wins", "losses"} <= columns:
        return None
    draws = "draws" if "draws" in columns else "0"
    phase = " AND phase = 'regular'" if "phase" in columns else ""
    try:
        return _con.execute(
            f"SELECT wins, {draws}, losses FROM team_seasons "
            f"WHERE season = ? AND club_now = ?{phase} LIMIT 1",
            (season, club)).fetchone()
    except sqlite3.Error:
        return None


# ------------------------------------------------------ season overview

def season_overview(sport, con, season, club=None, nested=False) -> None:
    """One season, and optionally one club's part in it.

    `club` is whatever the clicked row named -- a club-season row from a
    player's career or a club's own record table -- and where it is given
    the leaders and the totals are that club's, with the champion still
    shown because that is the first thing anyone asks about a season.
    """
    V = sport.vocab
    revision = _revision(sport.db)
    season = int(season)

    winner = champion(sport.key, season, revision, con)
    heading = f"{season} {V.season}"
    if club:
        heading += f" — {club}"
    st.markdown(f"### {heading}")

    logo_club = club or winner
    logo = logo_for(sport, con, logo_club)
    badge, facts = (st.columns([1, 4]) if logo else (None, st.container()))
    if logo:
        badge.markdown(logo_html(logo), unsafe_allow_html=True)
        badge.caption(logo_club)

    with facts:
        players, clubs = _season_totals(
            sport.key, season, club, revision, con)
        tiles = []
        if winner:
            tiles.append((V.title.capitalize(), winner))
        if club:
            record = _club_season_record(sport.key, season, club, revision, con)
            if record:
                won, drew, lost = record
                tiles.append(("Record",
                              f"{won or 0}-{drew or 0}-{lost or 0}"))
        tiles.append(("Players", f"{players:,}"))
        if not club:
            tiles.append((f"{V.clubs.capitalize()}", f"{clubs:,}"))
        columns = st.columns(len(tiles))
        for column, (label, value) in zip(columns, tiles):
            column.markdown(f"<div class='count'>{value}</div>"
                            f"<div class='count-label'>{label}</div>",
                            unsafe_allow_html=True)
        if winner is None:
            st.caption(
                f"This database does not record a {V.title} winner for "
                f"{sport.label.replace(' Data Lab', '')}.")

    leaders = _leaders(sport.key, season, club, revision, con)
    if not leaders.empty:
        st.markdown(f"**Leading {V.score} in {season}**")
        st.caption(f"Every {V.game} recorded for the {V.season}, "
                   f"{V.postseason} included.")
        import components
        components.clickable_player_table(
            leaders.drop(columns=["PlayerID"]), leaders["PlayerID"].tolist(),
            sport, con, key=f"overlay_season_leaders_{season}_{club}",
            nested=nested, height=min(38 * (len(leaders) + 1) + 3, 420))

    standings = _standings(sport.key, season, revision, con)
    if not standings.empty:
        with st.expander(f"{season} {V.club} records ({len(standings)})"):
            import components
            club_column = next(
                (column for column in ("Club", "Team")
                 if column in standings.columns), standings.columns[0])
            components.clickable_club_table(
                standings, standings[club_column].tolist(), sport, con,
                key=f"overlay_season_standings_{season}", nested=nested)

    season_notes(sport, season)

    awards = _season_awards(sport.key, season, revision, con)
    if not awards.empty:
        if club and "Club" in awards.columns:
            mine = awards[awards["Club"].astype(str).str.contains(
                str(club), case=False, na=False)]
            if not mine.empty:
                st.markdown(f"**{club} award winners**")
                import components
                ids = (mine["PlayerID"].tolist() if "PlayerID" in mine
                       else [None] * len(mine))
                components.clickable_player_table(
                    mine.drop(columns=["PlayerID"], errors="ignore"), ids,
                    sport, con, key=f"overlay_season_mine_{season}_{club}",
                    nested=nested)
        with st.expander(f"{season} award winners ({len(awards)})"):
            import components
            ids = (awards["PlayerID"].tolist() if "PlayerID" in awards
                   else [None] * len(awards))
            components.clickable_player_table(
                awards.drop(columns=["PlayerID"], errors="ignore"), ids,
                sport, con, key=f"overlay_season_awards_{season}",
                nested=nested)


# -------------------------------------------------- round / venue overview

def _round_key(value) -> str:
    text = str(value).strip().upper()
    return text[1:] if text.startswith("R") and text[1:].isdigit() else text


@st.cache_data(show_spinner=False)
def _round_results(season, round_value, revision, _con) -> pd.DataFrame:
    if not _has_table(_con, "matches"):
        return pd.DataFrame()
    key = _round_key(round_value)
    try:
        return pd.read_sql_query(
            """SELECT match_id AS match_id,
                      season AS Season, round AS Round, match_date AS Date,
                      home_team AS Home, away_team AS Away,
                      CAST(home_score AS INTEGER) || '–' ||
                        CAST(away_score AS INTEGER) AS Score,
                      CASE WHEN home_q1 IS NOT NULL THEN home_q1 || '–' || away_q1 END AS Q1,
                      CASE WHEN home_q2 IS NOT NULL THEN home_q2 || '–' || away_q2 END AS Q2,
                      CASE WHEN home_q3 IS NOT NULL THEN home_q3 || '–' || away_q3 END AS Q3,
                      CASE WHEN home_q4 IS NOT NULL THEN home_q4 || '–' || away_q4 END AS Q4,
                      ABS(CAST(home_score AS INTEGER)-CAST(away_score AS INTEGER)) AS Margin,
                      venue AS Venue, attendance AS Crowd,
                      CASE data_status WHEN 'player_stats' THEN 'Complete'
                        WHEN 'partial_player_stats' THEN 'Partial player stats'
                        WHEN 'score_only' THEN 'Score only' ELSE data_status END AS Status
                 FROM matches
                WHERE season=? AND UPPER(CASE
                      WHEN round GLOB 'R[0-9]*' THEN SUBSTR(round,2)
                      ELSE round END)=?
                ORDER BY match_date, home_team""",
            _con, params=(int(season), key),
        ).dropna(axis=1, how="all")
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _round_votes(season, round_value, revision, _con) -> pd.DataFrame:
    key = _round_key(round_value)
    if not key.isdigit() or not _has_table(_con, "brownlow_round_votes"):
        return pd.DataFrame()
    try:
        return pd.read_sql_query(
            """SELECT r.player AS Player, r.clubs AS Club, v.votes AS Votes,
                      r.player_id AS PlayerID
                 FROM brownlow_round_votes v
                 JOIN brownlow_results r ON r.result_id=v.result_id
                WHERE v.season=? AND v.round_number=? AND v.votes>0
                  AND r.match_status IN ('unique','resolved')
                ORDER BY v.votes DESC, r.player""",
            _con, params=(int(season), int(key)),
        )
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


def round_overview(sport, con, season, round_value, nested=False) -> None:
    """A round card combining every result with available award voting."""
    season, key = int(season), _round_key(round_value)
    label = f"Round {key}" if key.isdigit() else key
    st.markdown(f"### {season} · {label}")
    round_caveat(sport, season, key)
    revision = _revision(sport.db)
    results = _round_results(season, key, revision, con)
    votes = _round_votes(season, key, revision, con)
    if results.empty:
        st.info("No match results are recorded for this round.")
        return
    crowds = pd.to_numeric(results.get("Crowd"), errors="coerce").dropna()
    c1, c2, c3 = st.columns(3)
    c1.metric("Matches", f"{len(results):,}")
    c2.metric("Venues", f"{results['Venue'].nunique():,}")
    c3.metric("Total crowd", f"{int(crowds.sum()):,}" if len(crowds) else "—")
    st.markdown("**Results**")
    st.caption(f"Open a {sport.vocab.game} for its scoring progression, "
               "both box scores and everything else recorded about it.")
    import components
    shown = results.drop(columns=["match_id"], errors="ignore")
    components.clickable_match_table(
        shown, results.to_dict("records"),
        key=f"overlay_round_results_{season}_{key}", sport=sport, con=con,
        nested=nested,
        column_config={"Crowd": st.column_config.NumberColumn(format="%d")})
    if not votes.empty:
        st.markdown("**Brownlow votes**")
        st.caption("Round totals from the AFL Tables Brownlow count.")
        components.clickable_player_table(
            votes.drop(columns=["PlayerID"]), votes["PlayerID"].tolist(),
            sport, con, key=f"overlay_round_votes_{season}_{key}",
            nested=nested)
    elif key.isdigit():
        st.caption("No Brownlow round votes are available for this season and round.")


def _venue_candidates(sport, venue) -> list[str]:
    candidates = [str(venue).strip()]
    display = getattr(sport, "venue_display", {}) or {}
    candidates.extend(raw for raw, shown in display.items()
                      if str(shown).casefold() == str(venue).strip().casefold())
    try:
        candidates.append(sport.schema.canonical_venue(venue))
    except Exception:
        pass
    return list(dict.fromkeys(value for value in candidates if value))


@st.cache_data(show_spinner=False)
def _venue_data(sport_key, venue, candidates, revision, _con):
    marks = ",".join("?" for _ in candidates)
    summary = None
    if _has_table(_con, "venue_summary"):
        summary = _con.execute(
            f"SELECT * FROM venue_summary WHERE LOWER(venue) IN ({marks}) LIMIT 1",
            tuple(value.casefold() for value in candidates)).fetchone()
    resolved = summary[0] if summary else venue
    frames = {}
    queries = {
        "teams": """SELECT team AS Club, played AS P, wins AS W, draws AS D,
                    losses AS L, percentage AS '%', win_percentage AS 'Win %',
                    scores_100_for AS '100+ for', scores_100_against AS '100+ against'
                    FROM venue_team_records WHERE venue=? ORDER BY rank""",
        "records": """SELECT CASE category WHEN 'biggest_win' THEN 'Biggest win'
                    WHEN 'highest_score' THEN 'Highest score' ELSE 'Lowest score' END AS Record,
                    record_value AS Value, team AS Team, team_score AS Score,
                    opponent AS Opponent, opponent_score AS 'Opponent score', match_date AS Date
                    FROM venue_match_records WHERE venue=? ORDER BY category, rank""",
        "leaders": """SELECT CASE category WHEN 'most_games' THEN 'Games'
                    ELSE 'Goals' END AS Record, record_value AS Value,
                    player AS Player, clubs AS Clubs FROM venue_player_records
                    WHERE venue=? ORDER BY category, rank""",
        "single": """SELECT CASE category WHEN 'most_goals_game' THEN 'Goals in a game'
                    ELSE 'Disposals in a game' END AS Record,
                    record_value AS Value, player AS Player,
                    match_description AS Match FROM venue_player_game_records
                    WHERE venue=? ORDER BY category, rank""",
    }
    for name, query in queries.items():
        try:
            frames[name] = pd.read_sql_query(query, _con, params=(resolved,))
        except (sqlite3.Error, pd.errors.DatabaseError):
            frames[name] = pd.DataFrame()
    try:
        recent = pd.read_sql_query(
            """SELECT match_id AS match_id,
                      season AS Season, round AS Round, match_date AS Date,
                      home_team AS Home, away_team AS Away,
                      CAST(home_score AS INTEGER) || '–' || CAST(away_score AS INTEGER) AS Score,
                      venue AS Venue, attendance AS Crowd
                 FROM matches WHERE LOWER(venue) IN (""" + marks + ") "
            "ORDER BY match_date DESC LIMIT 20",
            _con, params=tuple(value.casefold() for value in candidates))
    except (sqlite3.Error, pd.errors.DatabaseError):
        recent = pd.DataFrame()
    return summary, resolved, frames, recent


def venue_overview(sport, con, venue, nested=False) -> None:
    """Venue card with AFL Tables history, records, leaders and matches."""
    candidates = _venue_candidates(sport, venue)
    summary, resolved, frames, recent = _venue_data(
        sport.key, venue, tuple(candidates), _revision(sport.db), con)
    st.markdown(f"### {resolved}")
    if summary:
        (_name, in_use, games, _goals, _behinds, _points, average,
         scores_100, profile_url, _source_url) = summary
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Used", in_use or "—")
        c2.metric("Matches", f"{games:,}" if games is not None else "—")
        c3.metric("Average score", f"{average:.2f}" if average is not None else "—")
        c4.metric("Scores of 100+", f"{scores_100:,}" if scores_100 is not None else "—")
        st.link_button("AFL Tables source", profile_url,
                       icon=":material/open_in_new:", type="tertiary")
    else:
        st.caption("No AFL Tables venue profile is loaded for this ground.")

    import components
    if not recent.empty:
        st.markdown("**Latest matches**")
        components.clickable_match_table(
            recent.drop(columns=["match_id"], errors="ignore"),
            recent.to_dict("records"),
            key=f"overlay_venue_recent_{resolved}", sport=sport, con=con,
            nested=nested,
            column_config={"Crowd": st.column_config.NumberColumn(format="%d")})
    if not frames["teams"].empty:
        with st.expander(f"Club records ({len(frames['teams'])})"):
            components.clickable_entity_table(
                frames["teams"], sport, con,
                key=f"overlay_venue_teams_{resolved}", nested=nested,
                column_config={"%": st.column_config.NumberColumn(format="%.2f"),
                               "Win %": st.column_config.NumberColumn(format="%.2f")})
    for label, key in (("Record matches", "records"),
                       ("Career leaders", "leaders"),
                       ("Single-game leaders", "single")):
        if not frames[key].empty:
            with st.expander(f"{label} ({len(frames[key])})"):
                st.dataframe(frames[key], hide_index=True, width="stretch")


# -------------------------------------------------------- club overview

@st.cache_data(show_spinner=False)
def _club_summary(sport_key, club, revision, _con):
    """(first season, last season, players) for one club, or None."""
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    try:
        return _con.execute(
            f"SELECT MIN({sc.season}), MAX({sc.season}), "
            f"       COUNT(DISTINCT {sc.player_id}) "
            f"FROM {sc.games} "
            f"WHERE {sc.club_hist} = ? OR {sc.club_now} = ?",
            (club, club)).fetchone()
    except sqlite3.Error:
        return None


@st.cache_data(show_spinner=False)
def _club_titles(sport_key, club, revision, _con) -> list:
    """Seasons this club won the title, as far as the data can say."""
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    if sport.title_round:
        try:
            return [row[0] for row in _con.execute(
                f"SELECT DISTINCT {sc.season} FROM {sc.games} "
                f"WHERE UPPER(TRIM({sc.round})) = ? AND {sc.result} = 'W' "
                f"AND ({sc.club_hist} = ? OR {sc.club_now} = ?) "
                f"ORDER BY {sc.season}",
                (sport.title_round.upper(), club, club))]
        except sqlite3.Error:
            return []
    if "champion" in _columns(_con, "team_seasons"):
        try:
            return [row[0] for row in _con.execute(
                "SELECT DISTINCT season FROM team_seasons "
                "WHERE champion = 1 AND club_now = ? ORDER BY season",
                (club,))]
        except sqlite3.Error:
            return []
    return []


@st.cache_data(show_spinner=False)
def _club_leaders(sport_key, club, revision, _con) -> pd.DataFrame:
    import sports

    sport = sports.get(sport_key)
    sc, V = sport.schema, sport.vocab
    try:
        return pd.read_sql_query(
            f"SELECT g.{sc.player_id} AS PlayerID, "
            f"       g.{sc.player} AS Player, "
            f"       MIN(g.{sc.season}) || '–' || MAX(g.{sc.season}) AS Span, "
            f"       SUM(g.{sc.game_score}) AS \"{V.score.capitalize()}\" "
            f"FROM {sc.games} g "
            f"WHERE g.{sc.club_hist} = ? OR g.{sc.club_now} = ? "
            f"GROUP BY g.{sc.player_id} "
            f"ORDER BY SUM(g.{sc.game_score}) DESC LIMIT 10",
            _con, params=(club, club))
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _club_identity(club, revision, _con):
    """Resolve a display/game name to the optional clubs-table row."""
    columns = _columns(_con, "clubs")
    if not {"club_id", "name"} <= columns:
        return None
    comparisons = ["LOWER(name) = LOWER(?)", "LOWER(club_id) = LOWER(?)"]
    params = [club, club]
    if "db_club_now" in columns:
        comparisons.append("LOWER(db_club_now) = LOWER(?)")
        params.append(club)
    select = ["club_id", "name"]
    for column in ("abbreviation", "db_club_now", "wikipedia_url"):
        select.append(column if column in columns else "NULL")
    try:
        return _con.execute(
            f"SELECT {', '.join(select)} FROM clubs WHERE "
            + " OR ".join(comparisons) + " LIMIT 1",
            params,
        ).fetchone()
    except sqlite3.Error:
        return None


@st.cache_data(show_spinner=False)
def _club_information(club_id, revision, _con) -> pd.DataFrame:
    columns = _columns(_con, "club_wikipedia_fields")
    if not club_id or not {"club_id", "field_value"} <= columns:
        return pd.DataFrame()
    label = ("field_label" if "field_label" in columns else
             "field_key" if "field_key" in columns else None)
    if label is None:
        return pd.DataFrame()
    try:
        return pd.read_sql_query(
            f"SELECT {label} AS Field, field_value AS Value "
            "FROM club_wikipedia_fields WHERE club_id = ? "
            f"ORDER BY {label}",
            _con, params=(club_id,),
        )
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _club_recent_games(club, club_id, revision, _con) -> pd.DataFrame:
    columns = _columns(_con, "club_match_sources")
    required = {"source_club_id", "season", "opponent_raw", "result"}
    if not required <= columns:
        return pd.DataFrame()
    candidates = [str(value) for value in (club_id, club) if value]
    where = [f"source_club_id IN ({','.join('?' for _ in candidates)})"]
    params = list(candidates)
    if "source_club_label" in columns:
        where.append("LOWER(source_club_label) = LOWER(?)")
        params.append(club)
    fields = [
        "season AS Season",
        ("round AS Round" if "round" in columns else "NULL AS Round"),
        ("match_date AS Date" if "match_date" in columns else "NULL AS Date"),
        "opponent_raw AS Opponent",
        "result AS Result",
        ("margin AS Margin" if "margin" in columns else "NULL AS Margin"),
        ("scoring_for_raw AS Score" if "scoring_for_raw" in columns
         else "NULL AS Score"),
        ("venue_raw AS Venue" if "venue_raw" in columns else "NULL AS Venue"),
    ]
    order = "match_date" if "match_date" in columns else "season"
    try:
        return pd.read_sql_query(
            f"SELECT {', '.join(fields)} FROM club_match_sources "
            f"WHERE ({' OR '.join(where)}) ORDER BY {order} DESC LIMIT 5",
            _con, params=tuple(params),
        ).dropna(axis=1, how="all")
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _club_award_winners(club, revision, _con) -> pd.DataFrame:
    columns = _columns(_con, "awards")
    if "club" not in columns or "season" not in columns:
        return pd.DataFrame()
    award = "award_name" if "award_name" in columns else (
        "award" if "award" in columns else None)
    player = "player" if "player" in columns else None
    if not award or not player:
        return pd.DataFrame()
    player_id = "NULL AS PlayerID"
    if "player_id" in columns:
        player_id = "player_id AS PlayerID"
    elif "dg_person_id" in columns and _columns(_con, "person_links"):
        player_id = (
            "(SELECT player_id FROM person_links l "
            "WHERE l.dg_person_id = awards.dg_person_id "
            "AND l.match_status IN ('from_draft','unique','resolved') "
            "LIMIT 1) AS PlayerID"
        )
    try:
        return pd.read_sql_query(
            f"SELECT season AS Season, {award} AS Award, {player} AS Player, "
            f"{player_id} "
            "FROM awards WHERE LOWER(COALESCE(club, '')) = LOWER(?) "
            "OR ',' || LOWER(COALESCE(club, '')) || ',' LIKE "
            "'%,' || LOWER(?) || ',%' "
            "ORDER BY season DESC, Award, Player LIMIT 5",
            _con, params=(club, club),
        )
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _club_hall_of_famers(club, revision, _con) -> pd.DataFrame:
    columns = _columns(_con, "hall_of_fame")
    if not {"club", "name"} <= columns:
        return pd.DataFrame()
    inducted = ("inducted_year AS Inducted" if "inducted_year" in columns
                else "NULL AS Inducted")
    legend = ("CASE WHEN is_legend = 1 THEN 'Legend' ELSE '' END AS Status"
              if "is_legend" in columns else "NULL AS Status")
    try:
        return pd.read_sql_query(
            f"SELECT name AS Name, {inducted}, {legend}, "
            + ("player_id AS PlayerID " if "player_id" in columns
               else "NULL AS PlayerID ")
            + "FROM hall_of_fame "
            "WHERE LOWER(COALESCE(club, '')) LIKE '%' || LOWER(?) || '%' "
            "ORDER BY Inducted DESC, Name LIMIT 5",
            _con, params=(club,),
        ).dropna(axis=1, how="all")
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


def club_overview(sport, con, club, nested=False) -> None:
    """One club's identity, history, recent form, honours and leaders."""
    V = sport.vocab
    revision = _revision(sport.db)
    identity = _club_identity(club, revision, con)
    club_id = identity[0] if identity else None
    display_name = identity[1] if identity else club

    logo = logo_for(sport, con, display_name)
    if logo:
        badge, title = st.columns([1, 5])
        badge.markdown(logo_html(logo), unsafe_allow_html=True)
        title.markdown(f"### {display_name}")
        if identity and identity[2]:
            title.caption(identity[2])
    else:
        st.markdown(f"### {display_name}")

    games_name = identity[3] if identity and identity[3] else club
    summary = _club_summary(sport.key, games_name, revision, con)
    titles = _club_titles(sport.key, games_name, revision, con)
    if summary and summary[0] is not None:
        first, last, players = summary
        tiles = [
            (V.season.capitalize() + "s", f"{first}–{last}"),
            ("Players", f"{players:,}"),
            (V.title_plural, f"{len(titles)}"),
        ]
        columns = st.columns(len(tiles))
        for column, (label, value) in zip(columns, tiles):
            column.markdown(f"<div class='count'>{value}</div>"
                            f"<div class='count-label'>{label}</div>",
                            unsafe_allow_html=True)
    else:
        st.caption(f"No {V.games} recorded for {display_name} in this database.")

    information = _club_information(club_id, revision, con)
    if not information.empty:
        preferred = {
            "Full name", "Nickname(s)", "Founded", "Colours", "Coach",
            "Captain(s)", "Ground(s)", "Home ground", "Competition",
        }
        headline = information[information["Field"].isin(preferred)].head(6)
        if not headline.empty:
            st.markdown("**Club information**")
            st.dataframe(headline, hide_index=True, width="stretch")
        with st.expander(f"All club information ({len(information)})"):
            st.dataframe(information, hide_index=True, width="stretch")

    recent = _club_recent_games(display_name, club_id, revision, con)
    if not recent.empty:
        st.markdown(f"**Last {len(recent)} {V.games}**")
        import components
        components.clickable_game_table(
            recent, sport, con, key=f"overlay_club_games_{club_id or club}",
            nested=nested)

    if titles:
        st.markdown(f"**{V.title_plural} ({len(titles)})**")
        st.write(", ".join(str(season) for season in reversed(titles)))

    award_winners = _club_award_winners(display_name, revision, con)
    if not award_winners.empty:
        st.markdown("**Latest 5 award winners**")
        import components
        components.clickable_player_table(
            award_winners.drop(columns=["PlayerID"]),
            award_winners["PlayerID"].tolist(), sport, con,
            key=f"overlay_club_awards_{club_id or club}", nested=nested)

    hall = _club_hall_of_famers(display_name, revision, con)
    if not hall.empty:
        st.markdown("**Latest 5 Hall of Fame inductees**")
        import components
        components.clickable_player_table(
            hall.drop(columns=["PlayerID"]), hall["PlayerID"].tolist(),
            sport, con, key=f"overlay_club_hof_{club_id or club}",
            nested=nested)

    leaders = _club_leaders(sport.key, games_name, revision, con)
    if not leaders.empty:
        st.markdown(f"**Leading {V.score}, all time**")
        import components
        components.clickable_player_table(
            leaders.drop(columns=["PlayerID"]), leaders["PlayerID"].tolist(),
            sport, con, key=f"overlay_club_leaders_{club_id or club}",
            nested=nested)


# --------------------------------------------------------- match record
#
# A results row names two clubs and a score. The database holds a great
# deal more about the same match -- the score at every break, every
# player's line on both sides, the crowd, the conditions -- and until now
# none of it was reachable, because the only way in was a card built from
# the columns the results table had already shown.
#
# Everything below is built from the schema rather than from a sport, so
# one card serves all four: the box score is whatever `games` columns the
# sport declares and this match actually filled in, the period-by-period
# score appears only where a table records one, and a sport with no
# per-match table at all falls back to the result row it was opened from.


def _field(record, *names, default=None):
    """First present, non-empty value among `names`.

    `record` is a `club_history.Match`, a `matches` row as a dict, or the
    mapping a results table built -- three shapes that all answer to a
    field name and none of which share an accessor.
    """
    for name in names:
        if isinstance(record, Mapping):
            if name not in record:
                continue
            value = record[name]
        elif hasattr(record, name):
            value = getattr(record, name)
        else:
            continue
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def _match_keys(match) -> tuple:
    """Every id this match might be found by, most specific first.

    The AFL's club-history rows carry a resolved `match_id`; the other
    three sports leave it null and carry the source's own game key, which
    is what their `matches` table is keyed on.
    """
    keys = []
    for name in ("match_id", "game_id", "source_game_key"):
        value = _field(match, name)
        if value is not None and value not in keys:
            keys.append(value)
    return tuple(keys)


@st.cache_data(show_spinner=False)
def _match_row(sport_key, keys, revision, _con) -> dict:
    """The per-match row for one match, or {} when the sport has none."""
    import sports

    sc = sports.get(sport_key).schema
    if not sc.matches or not keys or not _has_table(_con, sc.matches):
        return {}
    for key in keys:
        try:
            cursor = _con.execute(
                f"SELECT * FROM {sc.matches} WHERE {sc.match_key} = ? LIMIT 1",
                (key,))
            row = cursor.fetchone()
        except sqlite3.Error:
            return {}
        if row is not None:
            return dict(zip([column[0] for column in cursor.description], row))
    return {}


@st.cache_data(show_spinner=False)
def _match_detail_row(sport_key, key, revision, _con) -> dict:
    """An optional second per-match table, keyed the same way.

    The AFL build splits what AFL Tables prints at the top of a match page
    -- the scoring at every break, the start time, how many sources agreed
    -- into `match_details`. No other sport has one, and none has to.
    """
    import sports

    sc = sports.get(sport_key).schema
    if key is None or not _has_table(_con, "match_details"):
        return {}
    try:
        cursor = _con.execute(
            f"SELECT * FROM match_details WHERE {sc.match_key} = ? LIMIT 1",
            (key,))
        row = cursor.fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    return dict(zip([column[0] for column in cursor.description], row))


@st.cache_data(show_spinner=False)
def _box_score(sport_key, key, revision, _con) -> pd.DataFrame:
    """Every player's line in one match, both sides, in schema order.

    Columns the sport declares but this match never filled are dropped
    rather than shown empty: disposals were not counted before 1965, and a
    column of blanks reads as a database fault rather than as a statistic
    the game did not yet keep.
    """
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    columns = _columns(_con, sc.games)
    if key is None or not columns or sc.games_match_key not in columns:
        return pd.DataFrame()
    stats = [stat for stat in sc.box_score_stats() if stat in columns]
    select = [f"{sc.player_id} AS PlayerID", f"{sc.player} AS Player",
              f"{sc.club_hist} AS Club"]
    if sc.games_home_flag and sc.games_home_flag in columns:
        select.append(f"{sc.games_home_flag} AS HomeFlag")
    if sc.games_side_key in columns:
        select.append(f"{sc.games_side_key} AS SideKey")
    select += [f'{stat} AS "{stat}"' for stat in stats]
    try:
        frame = pd.read_sql_query(
            f"SELECT {', '.join(select)} FROM {sc.games} "
            f"WHERE {sc.games_match_key} = ?", _con, params=(key,))
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()
    for stat in stats:
        if frame[stat].isna().all():
            frame = frame.drop(columns=[stat])
    return frame


def _sides(sport, match_row, match) -> list:
    """[(club, 'Home'|'Away'), ...] for the two teams, home first.

    Read from the match row where there is one, because that is the only
    place that records which side was at home. Where there is not -- the
    MLB, or an AFL final with no home team -- the two clubs still have to
    be named, and the row the reader clicked names them.
    """
    sc = sport.schema
    home = _field(match_row, sc.match_home_team)
    away = _field(match_row, sc.match_away_team)
    if home and away:
        return [(str(home), "Home"), (str(away), "Away")]
    first = _field(match, "club_id", "Home", "Club", "For")
    second = _field(match, "opponent_id", "Away", "Opponent")
    position = str(_field(match, "team_position", default="") or "")
    if position == "H":
        labelled = [(first, "Home"), (second, "Away")]
    elif position == "A":
        labelled = [(second, "Home"), (first, "Away")]
    else:
        labelled = [(first, ""), (second, "")]
    return [(str(club), role) for club, role in labelled if club]


def _side_of(frame, club, role) -> pd.DataFrame:
    """The box-score rows belonging to one side of the match.

    Three ways of saying the same thing, because three builds say it
    differently: an explicit home flag, a side key spelled the way the
    match table spells a club, or the club name the games rows carry.
    """
    if "HomeFlag" in frame.columns and role in ("Home", "Away"):
        flag = frame["HomeFlag"].fillna(-1).astype(int)
        chosen = frame[flag == (1 if role == "Home" else 0)]
        if not chosen.empty:
            return chosen
    for column in ("SideKey", "Club"):
        if column not in frame.columns:
            continue
        chosen = frame[frame[column].astype(str).str.casefold()
                       == str(club).casefold()]
        if not chosen.empty:
            return chosen
    return frame.iloc[0:0]


def _display_name(club, frame) -> str:
    """The fullest name this side is known by.

    The match row and the player rows do not always agree: nflverse's
    schedule says 'KC' and its player rows say 'Kansas City Chiefs'. The
    longer of the two is the one worth printing, and the one the logo
    index is keyed on.
    """
    if frame is None or frame.empty or "Club" not in frame.columns:
        return str(club)
    named = frame["Club"].dropna().astype(str)
    if named.empty:
        return str(club)
    from_games = named.mode().iat[0]
    return from_games if len(from_games) > len(str(club)) else str(club)


def _tidy(value):
    """A statistic as a reader writes it: 36, not 36.0, and 1,247 not 1247."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(number):
        return "—"
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.1f}"


def _as_int(value):
    """`'42762'` -> 42762. Attendance arrives as text from AFL Tables."""
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


#: `home_q3`, `away_q1_behinds` -- the running score, wherever it is kept.
_PERIOD_COLUMN = re.compile(r"^(home|away)_q\d+(_\w+)?$")


def _period_scores(sport, match_row, details, names=()) -> tuple:
    """(table, present) -- the score at each break, however it is recorded.

    Two sources, in order of richness. `match_details` holds goals and
    behinds as well as points, which is how a football score is actually
    written; `matches` holds running points alone. A sport that records
    neither gets no section rather than a table of dashes.
    """
    sc = sport.schema
    home, away = (list(names) + [None, None])[:2]
    home = home or str(_field(match_row, sc.match_home_team, default="Home"))
    away = away or str(_field(match_row, sc.match_away_team, default="Away"))

    def cumulative(source, template, count=9):
        rows = []
        for period in range(1, count + 1):
            values = {}
            for side in ("home", "away"):
                points = _as_int(_field(source, template.format(
                    side=side, n=period)))
                if points is None:
                    break
                goals = _as_int(_field(source, f"{side}_q{period}_goals"))
                behinds = _as_int(_field(source, f"{side}_q{period}_behinds"))
                values[side] = (points, goals, behinds)
            if len(values) != 2:
                break
            rows.append((period, values["home"], values["away"]))
        return rows

    rows = cumulative(details, "{side}_q{n}_points") if details else []
    if not rows:
        rows = cumulative(match_row, "{side}_q{n}")
    if not rows:
        return pd.DataFrame(), False

    def written(points, goals, behinds):
        if goals is None or behinds is None:
            return f"{points}"
        return f"{goals}.{behinds} ({points})"

    # A break is named for the period it closes, so four of them are
    # quarters and two are halves. The last one is not a break at all.
    period_word = "quarter" if len(rows) == 4 else "period"
    labelled = []
    previous = (0, 0)
    for index, (period, home_side, away_side) in enumerate(rows):
        last = index == len(rows) - 1
        name = "Full time" if last else f"Q{period}"
        if len(rows) == 2 and not last:
            name = "Half time"
        lead = home_side[0] - away_side[0]
        labelled.append({
            "Break": name,
            home: written(*home_side),
            away: written(*away_side),
            f"In the {period_word}": (f"{home_side[0] - previous[0]}"
                                      f"–{away_side[0] - previous[1]}"),
            "Lead": ("Scores level" if lead == 0 else
                     f"{home if lead > 0 else away} by {abs(lead)}"),
        })
        previous = (home_side[0], away_side[0])
    return pd.DataFrame(labelled), True


def _scoreboard(sport, con, clubs, roles, scores) -> str:
    """The two clubs, their logos and the score, as one block of HTML."""
    best = max((score for score in scores if score is not None), default=None)
    cells = []
    for club, role, score in zip(clubs, roles, scores):
        logo = logo_for(sport, con, club)
        badge = (logo_html(logo, height=54, alt=club) if logo else "")
        winning = (score is not None and best is not None and score == best
                   and scores.count(best) == 1)
        cells.append(
            f"<div class='score-side'>{badge}"
            f"<div class='score-club'>{html.escape(str(club))}</div>"
            + (f"<div class='score-role'>{html.escape(role)}</div>"
               if role else "")
            + "</div>")
        classes = "score-value is-winner" if winning else "score-value"
        cells.append(f"<div class='{classes}'>"
                     + ("—" if score is None else str(score)) + "</div>")
    # Sides outside, scores inside: home, home score, away score, away.
    if len(cells) == 4:
        cells = [cells[0], cells[1], cells[3], cells[2]]
    return f"<div class='scoreboard'>{''.join(cells)}</div>"


def _statbars(name_left, name_right, rows) -> str:
    """Team totals as mirrored bars, one statistic per line."""
    lines = []
    for label, left, right in rows:
        widest = max(abs(left), abs(right)) or 1
        left_share = 100 * abs(left) / widest
        right_share = 100 * abs(right) / widest
        lines.append(
            "<div class='statbar'>"
            f"<div class='statbar-num left'>{_tidy(left)}</div>"
            "<div class='statbar-track left'>"
            f"<div class='statbar-fill{' is-more' if left >= right else ''}' "
            f"style='width:{left_share:.0f}%'></div></div>"
            f"<div class='statbar-name'>{html.escape(label)}</div>"
            "<div class='statbar-track'>"
            f"<div class='statbar-fill{' is-more' if right >= left else ''}' "
            f"style='width:{right_share:.0f}%'></div></div>"
            f"<div class='statbar-num right'>{_tidy(right)}</div>"
            "</div>")
    header = (f"<div class='statbar'><div class='statbar-num left'></div>"
              f"<div class='statbar-track left' style='background:none'>"
              f"<div class='score-role' style='width:100%;text-align:right'>"
              f"{html.escape(name_left)}</div></div>"
              f"<div class='statbar-name'></div>"
              f"<div class='statbar-track' style='background:none'>"
              f"<div class='score-role'>{html.escape(name_right)}</div></div>"
              f"<div class='statbar-num right'></div></div>")
    return header + "".join(lines)


def _scorers(frame, stat) -> str:
    """`'Riley Thilthorpe 4, Izak Rankine 1'` for one side, or ''.

    Named for whatever the sport calls its score, so this is the
    goalkickers in the AFL, the try-scorers in the NFL and simply the
    leading scorers in the NBA.
    """
    if stat not in frame.columns:
        return ""
    scored = frame[pd.to_numeric(frame[stat], errors="coerce").fillna(0) > 0]
    if scored.empty:
        return ""
    scored = scored.sort_values(stat, ascending=False)
    return ", ".join(f"{row.Player} {_tidy(getattr(row, stat))}"
                     for row in scored.itertuples())


def match_overview(sport, con, match, nested=False) -> None:
    """One match, in as much detail as this sport's data can give.

    `match` is whatever the table that was clicked holds -- a
    `club_history.Match`, or a mapping of the columns a round card showed.
    Its ids are used to find the match's own row; everything the row
    cannot supply falls back to what the caller already had, so a card
    still renders for a sport with no per-match table at all.
    """
    V = sport.vocab
    sc = sport.schema
    revision = _revision(sport.db)

    keys = _match_keys(match)
    match_row = _match_row(sport.key, keys, revision, con)
    key = _field(match_row, sc.match_key, default=keys[0] if keys else None)
    details = _match_detail_row(sport.key, key, revision, con)

    sides = _sides(sport, match_row, match)
    scores = [_as_int(_field(match_row, sc.match_home_score)),
              _as_int(_field(match_row, sc.match_away_score))]
    if scores[0] is None or scores[1] is None:
        # A results row is one club's view: points_for is that club's.
        mine = _as_int(_field(match, "points_for"))
        theirs = _as_int(_field(match, "points_against"))
        position = str(_field(match, "team_position", default="") or "")
        scores = ([theirs, mine] if position == "A" else [mine, theirs])
    if len(sides) < 2:
        st.info(f"This {V.game} could not be resolved to two "
                f"{V.clubs} in the database.")
        return

    box = _box_score(sport.key, key, revision, con)
    stats = [column for column in box.columns
             if column not in ("PlayerID", "Player", "Club", "HomeFlag",
                               "SideKey")]
    per_side = [_side_of(box, club, role) for club, role in sides]
    # What to *call* each side, which is not always what the match row
    # calls it. nflverse's schedule says 'KC' and its player rows say
    # 'Kansas City Chiefs'; the second is the name a reader wants, and the
    # one the logo index is keyed on.
    names = [_display_name(club, frame)
             for (club, _), frame in zip(sides, per_side)]

    season = _field(match_row, sc.season) or _field(match, "season", "Season")
    round_value = (_field(match, "round", "Round")
                   or _field(match_row, sc.match_round))
    venue = (_field(match, "venue", "Venue")
             or _field(match_row, sc.match_venue))
    date = (_field(match_row, sc.match_date)
            or _field(match, "match_date", "Date", "date"))
    crowd = _as_int(_field(match_row, sc.match_attendance)
                    if sc.match_attendance else None) \
        or _as_int(_field(details, "attendance")) \
        or _as_int(_field(match, "attendance", "Crowd"))

    st.markdown(f"### {names[0]} vs {names[1]}")
    st.markdown(_scoreboard(sport, con, names,
                            [role for _, role in sides], scores),
                unsafe_allow_html=True)

    # The verdict line, because "63–54" does not say who won by how much.
    if scores[0] is not None and scores[1] is not None:
        margin = scores[0] - scores[1]
        winner = names[0] if margin > 0 else names[1]
        verdict = ("Scores were level" if margin == 0 else
                   f"<b>{html.escape(str(winner))}</b> won by "
                   f"{abs(margin)}")
        context = " · ".join(str(part) for part in (
            season, f"Round {round_value}" if str(round_value).isdigit()
            else round_value) if part)
        st.markdown(f"<div class='score-verdict'>{verdict}"
                    + (f" &nbsp;·&nbsp; {html.escape(context)}"
                       if context else "")
                    + "</div>", unsafe_allow_html=True)

    tiles = []
    if venue:
        # The name the ground goes by now, where the sport declares one:
        # the source says 'Docklands' and everybody else says Marvel
        # Stadium. The raw name is still what the venue link resolves on.
        shown_venue = (getattr(sport, "venue_display", {}) or {}).get(
            str(venue).strip(), str(venue))
        tiles.append((V.venue.capitalize(), shown_venue))
    if date:
        start = _field(details, "match_time")
        tiles.append(("Date", f"{date}" + (f" · {start}" if start else "")))
    if crowd:
        tiles.append(("Crowd", f"{crowd:,}"))
    if tiles:
        columns = st.columns(len(tiles))
        for column, (label, value) in zip(columns, tiles):
            column.markdown(
                f"<div class='count' style='font-size:1.5rem'>"
                f"{html.escape(value)}</div>"
                f"<div class='count-label'>{label}</div>",
                unsafe_allow_html=True)

    round_caveat(sport, season, round_value)

    import components
    components.card_links(
        sport, con, key_prefix="match_card", season=season,
        round_value=round_value, venue=venue, clubs=names)

    # -- the score at every break -------------------------------------
    periods, has_periods = _period_scores(sport, match_row, details, names)
    if has_periods:
        st.markdown("**Scoring progression**")
        st.caption("Running score at each break, with what each side put on "
                   "in between.")
        st.dataframe(periods, hide_index=True, width="stretch")

    # -- both sides, statistic by statistic ---------------------------
    if box.empty or not stats:
        st.caption(
            f"No per-player record is stored for this {V.game}. "
            + (f"{sport.label.replace(' Data Lab', '')} {V.games} are "
               f"recorded as {sport.games_row_label.lower()}, not as box "
               "scores." if sport.games_row_label else
               "The score is recorded but the teams are not."))
    else:
        left, right = per_side
        if not left.empty and not right.empty:
            totals = [(labels.title(stat),
                       float(pd.to_numeric(left[stat],
                                           errors="coerce").sum()),
                       float(pd.to_numeric(right[stat],
                                           errors="coerce").sum()))
                      for stat in stats]
            totals = [row for row in totals if row[1] or row[2]]
            if totals:
                st.markdown(f"**{V.club.capitalize()} totals**")
                # Ten bars is a comparison; twenty-two is a wall. The rest
                # stay one click away rather than being dropped, because
                # which ten matter depends on who is reading.
                st.markdown(_statbars(names[0], names[1], totals[:10]),
                            unsafe_allow_html=True)
                if len(totals) > 10:
                    with st.expander(f"All {len(totals)} team totals"):
                        st.dataframe(pd.DataFrame(
                            [{"Statistic": stat, names[0]: mine,
                              names[1]: theirs,
                              "Difference": mine - theirs}
                             for stat, mine, theirs in totals]),
                            hide_index=True, width="stretch")

            scorer_stat = sc.game_score if sc.game_score in stats else None
            if scorer_stat:
                st.markdown(f"**{V.score.capitalize()}**")
                for name, frame in zip(names, (left, right)):
                    scored = _scorers(frame, scorer_stat)
                    st.caption(f"**{name}** — {scored or 'none'}")

        st.markdown("**Box score**")
        st.caption(
            "Select a player to see their full career. "
            + (f"One statistic is recorded for this {V.game}."
               if len(stats) == 1 else
               f"{len(stats)} statistics are recorded for this {V.game}.")
            + " A statistic the competition was not yet keeping is left out "
              "rather than shown as a column of blanks.")
        tab_names = [names[0], names[1], "Both sides"]
        for tab, name, frame in zip(st.tabs(tab_names), tab_names,
                                    [left, right, box]):
            with tab:
                if frame.empty:
                    st.caption("No players are recorded for this side.")
                    continue
                _box_table(sport, con, frame, stats, key, name,
                           nested=nested)

    # -- everything else the row carries ------------------------------
    _match_source_links(match_row)
    shown = {sc.match_home_team, sc.match_away_team, sc.match_home_score,
             sc.match_away_score, sc.match_venue, sc.match_date,
             sc.match_attendance, sc.season, sc.match_key}
    extra = {key_: value for key_, value in (match_row or {}).items()
             if key_ not in shown and value is not None and str(value) != ""
             # The running score is the progression table above, not a
             # field; listing home_q1..away_q4 again is eight rows of noise.
             and not (has_periods and _PERIOD_COLUMN.match(key_))}
    if extra:
        with st.expander(f"Every recorded field for this {V.game}"):
            named = dict(sc.match_facts)
            st.dataframe(
                pd.DataFrame([{"Field": named.get(k, labels.title(k)),
                               "Value": str(v)} for k, v in extra.items()]),
                hide_index=True, width="stretch")


def _box_table(sport, con, frame, stats, key, name, nested=False) -> None:
    """One side's box score, sorted by the sport's leading statistic."""
    import components

    ordered = frame.sort_values(stats[0], ascending=False, kind="stable")
    display = ordered[["Player", "Club", *stats]].rename(
        columns={stat: labels.title(stat) for stat in stats})
    for column in display.columns[2:]:
        # A count arrives as REAL from SQLite and renders as "36.0".
        numeric = pd.to_numeric(display[column], errors="coerce")
        if numeric.notna().any() and (numeric.dropna() % 1 == 0).all():
            display[column] = numeric.astype("Int64")
    components.clickable_player_table(
        display, ordered["PlayerID"].tolist(), sport, con,
        key=f"match_box_{key}_{name}", nested=nested,
        height=min(38 * (len(display) + 1) + 3, 460))


def _match_source_links(match_row) -> None:
    """Link out to whatever the row cites as its source."""
    for column, value in (match_row or {}).items():
        if column.endswith("_url") and value:
            st.link_button(labels.title(column.removesuffix("_url")) + " source",
                           str(value), icon=":material/open_in_new:",
                           type="tertiary")


# ---------------------------------------------------------- game record

def game_card(sport, con, row, stat=None, nested=False) -> None:
    """One row of the games table, as a scorecard.

    `row` is a mapping of the columns a page already showed, which is why
    this takes a mapping rather than a player id and a date: the caller's
    frame is the record, and re-querying it would only risk showing
    something else.
    """
    V = sport.vocab
    get = (row.get if hasattr(row, "get")
           else lambda key, default=None: getattr(row, key, default))

    player = get("Player") or get("player") or "—"
    season = get("Season") or get("season")
    round_value = get("Rnd") or get("Round") or get("round")
    venue = get(V.venue.capitalize()) or get("Venue") or get("venue")
    st.markdown(f"### {player}")
    subtitle = " · ".join(str(v) for v in (
        season, round_value,
        get("For") or get("Club") or get("club_hist")) if v)
    if subtitle:
        st.caption(subtitle)

    if nested:
        import components
        components.card_links(
            sport, con, key_prefix="game_card_links",
            player_id=get("PlayerID") or get("player_id"), player=player,
            season=season, round_value=round_value, venue=venue,
            clubs=[get("For") or get("Club") or get("club_hist"),
                   get("Opponent") or get("opponent")],
        )

    tiles = []
    opponent = get("Opponent") or get("opponent")
    if opponent:
        tiles.append(("Opponent", opponent))
    if venue:
        tiles.append((V.venue.capitalize(), venue))
    result = get("Res") or get("Result") or get("result")
    if result:
        tiles.append(("Result", result))
    if stat and get(stat) is not None:
        tiles.append((labels.title(stat), get(stat)))
    if tiles:
        columns = st.columns(len(tiles))
        for column, (label, value) in zip(columns, tiles):
            column.markdown(f"<div class='count'>{value}</div>"
                            f"<div class='count-label'>{label}</div>",
                            unsafe_allow_html=True)

    # Values as text: a games row mixes ints, floats and strings, and a
    # mixed object column is what Arrow refuses to serialise.
    detail = {k: str(v) for k, v in dict(row).items()
              if v is not None and str(v) != ""}
    if detail:
        with st.expander("Every recorded field for this row"):
            st.dataframe(
                pd.DataFrame([{"Field": k, "Value": v}
                              for k, v in detail.items()]),
                hide_index=True, width="stretch")
