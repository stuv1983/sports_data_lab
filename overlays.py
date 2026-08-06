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
import mimetypes
import os
import pathlib
import sqlite3

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
    return index


def logo_for(sport, con, club) -> str | None:
    """The logo file for a club named as a results row names it."""
    if not club:
        return None
    index = _logo_index(sport.key, _revision(sport.db), con)
    return index.get(str(club).strip().lower())


def logo_html(path, height=64) -> str:
    """Inline the image as a data URI.

    Streamlit serves static files only from a configured folder and does
    not render SVG through st.image, and most of these logos are SVG.
    """
    data = pathlib.Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/svg+xml"
    return (f"<img src='data:{mime};base64,"
            f"{base64.b64encode(data).decode('ascii')}' "
            f"style='height:{height}px;width:auto;max-width:100%;'/>")


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
            f"SELECT g.{sc.player} AS Player, "
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
            return pd.read_sql_query(
                f"SELECT {', '.join(select)} FROM awards WHERE season = ? "
                "ORDER BY award_name, player", _con, params=(season,))
        if "award" in columns and "player_id" in columns:
            return pd.read_sql_query(
                "SELECT a.award AS Award, p.player AS Player "
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

def season_overview(sport, con, season, club=None) -> None:
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
        st.dataframe(leaders, hide_index=True, width="stretch",
                     height=min(38 * (len(leaders) + 1) + 3, 420))

    standings = _standings(sport.key, season, revision, con)
    if not standings.empty:
        with st.expander(f"{season} {V.club} records ({len(standings)})"):
            st.dataframe(standings, hide_index=True, width="stretch")

    awards = _season_awards(sport.key, season, revision, con)
    if not awards.empty:
        if club and "Club" in awards.columns:
            mine = awards[awards["Club"].astype(str).str.contains(
                str(club), case=False, na=False)]
            if not mine.empty:
                st.markdown(f"**{club} award winners**")
                st.dataframe(mine, hide_index=True, width="stretch")
        with st.expander(f"{season} award winners ({len(awards)})"):
            st.dataframe(awards, hide_index=True, width="stretch")


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
            f"SELECT g.{sc.player} AS Player, "
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
    try:
        return pd.read_sql_query(
            f"SELECT season AS Season, {award} AS Award, {player} AS Player "
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
            f"SELECT name AS Name, {inducted}, {legend} FROM hall_of_fame "
            "WHERE LOWER(COALESCE(club, '')) LIKE '%' || LOWER(?) || '%' "
            "ORDER BY Inducted DESC, Name LIMIT 5",
            _con, params=(club,),
        ).dropna(axis=1, how="all")
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


def club_overview(sport, con, club) -> None:
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
        st.dataframe(recent, hide_index=True, width="stretch")

    if titles:
        st.markdown(f"**{V.title_plural} ({len(titles)})**")
        st.write(", ".join(str(season) for season in reversed(titles)))

    award_winners = _club_award_winners(display_name, revision, con)
    if not award_winners.empty:
        st.markdown("**Latest 5 award winners**")
        st.dataframe(award_winners, hide_index=True, width="stretch")

    hall = _club_hall_of_famers(display_name, revision, con)
    if not hall.empty:
        st.markdown("**Latest 5 Hall of Fame inductees**")
        st.dataframe(hall, hide_index=True, width="stretch")

    leaders = _club_leaders(sport.key, games_name, revision, con)
    if not leaders.empty:
        st.markdown(f"**Leading {V.score}, all time**")
        st.dataframe(leaders, hide_index=True, width="stretch")


# ---------------------------------------------------------- game record

def game_card(sport, con, row, stat=None) -> None:
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
    st.markdown(f"### {player}")
    subtitle = " · ".join(str(v) for v in (
        season, get("Rnd") or get("Round") or get("round"),
        get("For") or get("Club") or get("club_hist")) if v)
    if subtitle:
        st.caption(subtitle)

    tiles = []
    opponent = get("Opponent") or get("opponent")
    if opponent:
        tiles.append(("Opponent", opponent))
    venue = get(V.venue.capitalize()) or get("Venue") or get("venue")
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
