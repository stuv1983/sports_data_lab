"""Streamlit Club Explorer for the optional 18-club data layer."""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pandas as pd
import streamlit as st

import components
import labels

from . import club_fields as CF
from . import club_history as CH
from . import club_logos as CL

REQUIRED_TABLES = {
    "clubs", "club_source_snapshots", "club_wikipedia_fields",
    "club_player_totals", "club_player_register", "club_player_records",
}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def club_data_available(con: sqlite3.Connection) -> bool:
    return REQUIRED_TABLES <= _tables(con) and bool(
        con.execute("SELECT 1 FROM clubs LIMIT 1").fetchone()
    )


def _read(con, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


#: Bookkeeping columns, never shown as a statistic.
_PLUMBING = {
    "row_id", "club_id", "player_name_source", "player_name", "player_url",
    "player_id", "match_status", "candidate_count", "raw_row_json",
    "imported_at",
}


#: Header spelling lives in labels.py now, so the Club Explorer, the Stats
#: Explorer and the grid all print a column the same way.
_label = labels.title


def _columns(con, table: str) -> list:
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


#: Link statuses whose player_id is trusted enough to open a career on.
#:
#: 'derived' belongs here and is the strongest of them: the NBA, MLB and
#: NFL club tables are built out of the player rows themselves by
#: utils/shared/derive_club_tables.py, so there was never a name to resolve. The
#: scraped AFL tables are the ones with 'unique' rows and, for a couple of
#: hundred of them, no id at all.
_TRUSTED = ("unique", "resolved", "from_draft", "derived")


def _split_links(frame: pd.DataFrame):
    """Pull the id column off a club table and return (frame, player_ids).

    A club table keeps the scraped name beside the id the linker resolved
    it to, and an unresolved row keeps a NULL id. Those rows stay in the
    table -- the player did play for the club -- they simply have no career
    to open, which clickable_player_table says for itself.
    """
    if "player_id" not in frame.columns:
        return frame, [None] * len(frame)
    ids = frame["player_id"]
    if "Link" in frame.columns:
        ids = ids.where(frame["Link"].isin(_TRUSTED))
    return (frame.drop(columns=["player_id"]),
            [None if pd.isna(v) else v for v in ids])


def _stat_selection(con, table: str) -> str:
    """The SELECT list for a club table, from whatever columns it has.

    The AFL's are kicks, marks and Brownlow votes; the NBA's are points and
    rebounds. Reading them off the table keeps one page serving every
    sport instead of one page per sport, each with its stats spelled out.
    """
    parts = ['player_name AS Player']
    for column in _columns(con, table):
        if column in _PLUMBING or column == "player_name":
            continue
        parts.append(f'{column} AS "{_label(column)}"')
    parts.append("match_status AS Link")
    parts.append("player_id")
    return ", ".join(parts)


def _drop_empty(frame: pd.DataFrame, keep=("Player",)) -> pd.DataFrame:
    """Hide columns no row filled in.

    The register carries cap numbers, jumper numbers and heights because
    the AFL scrape supplies them. A derived sport has none of that, and an
    all-blank column reads as missing data rather than as inapplicable.
    """
    empty = [c for c in frame.columns
             if c not in keep and frame[c].isna().all()]
    return frame.drop(columns=empty)


def _field_map(con, club_id: str) -> dict[str, str]:
    if "club_wikipedia_fields" not in _tables(con):
        return {}
    return dict(con.execute(
        "SELECT field_key, field_value FROM club_wikipedia_fields "
        "WHERE club_id=? ORDER BY field_key", (club_id,)
    ).fetchall())


def _first(fields: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = fields.get(key)
        if value:
            return value
    return "—"


def _headline(col, fields: dict[str, str], group: str, label: str) -> None:
    """One metric tile, with whatever was factored out shown beneath it.

    The raw infobox values are long enough that a tile truncates them --
    'Founded' rendered as '1885 ; 141 year…' and 'Home ground' as 'AFL:
    Melbourn…'. club_fields reduces each to a headline and returns the
    remainder, which goes in a caption instead of being lost.
    """
    value = CF.headline(fields, group)
    col.metric(label, value.primary or "—",
               help=value.raw or None)
    if value.qualifier:
        col.caption(value.qualifier)
    elif value.extras:
        col.caption(value.detail)


def _record_line(rec) -> str:
    pct = f"{rec.percentage:.1f}%" if rec.percentage is not None else "—"
    return (f"{rec.played} played · {rec.wins}-{rec.draws}-{rec.losses} "
            f"(W-D-L) · {pct}")


def _past_games_tab(con, sport, club_id: str, club_name: str,
                    games_name: str | None = None) -> None:
    """Searchable past results for one club, over the all-games rows.

    The source carries one row per club per match, so every figure here is
    from this club's point of view: `result` and `margin` are theirs, and
    the opponent comes from the other row of the same game rather than from
    any name matching.
    """
    if not CH.club_history_available(con):
        hint = sport.past_games_hint or "Load this sport's club-history rows."
        st.info(f"Past {sport.vocab.games} are not loaded. {hint}")
        return

    seasons = CH.seasons_available(con)
    if not seasons:
        st.info(f"No {sport.vocab.games} recorded for this club.")
        return

    # The club-history rows may be keyed by slug or by name depending on
    # which loader filled them; passing the wrong one silently produced a
    # 0-0-0 all-time record rather than saying anything was missing.
    club_id = CH.source_club_id(con, club_id, club_name)
    if club_id is None:
        st.info(f"No {sport.vocab.games} recorded for {club_name}.")
        return

    V = sport.vocab
    splits = {s.split: s for s in CH.home_away_splits(con, club_id)}
    cols = st.columns(4)
    all_time = CH.season_records(con, club_id, scope="all")
    played = sum(r.played for r in all_time)
    wins = sum(r.wins for r in all_time)
    draws = sum(r.draws for r in all_time)
    losses = sum(r.losses for r in all_time)
    cols[0].metric(V.games.capitalize(), f"{played:,}")
    cols[1].metric("Record", f"{wins}-{draws}-{losses}",
                   help="W-D-L, all time")
    for i, name in enumerate(("home", "away"), start=2):
        rec = splits.get(name)
        cols[i].metric(
            name.title(),
            f"{rec.wins}-{rec.draws}-{rec.losses}" if rec else "—",
            help=f"{rec.played} {V.games}" if rec else None)

    excluded = CH.excluded(con)
    if excluded:
        st.caption(
            "Excluded as not cleanly linked: "
            + ", ".join(f"{n:,} {status}" for status, n in excluded.items()))

    st.markdown(f"#### Search past {V.games}")
    f1, f2, f3 = st.columns(3)
    club_ids = CH.clubs_with_history(con)
    opponent = f1.selectbox(
        "Opponent", ["Any", *[c for c in club_ids if c != club_id]],
        format_func=lambda c: c if c == "Any" else c.replace("_", " ").title(),
        key=f"pg_opp_{club_id}")
    lo, hi = f2.select_slider(
        f"{V.season.capitalize()}s", options=sorted(seasons),
        value=(min(seasons), max(seasons)), key=f"pg_seasons_{club_id}")
    result = f3.selectbox(
        "Result", ["Any", "W", "D", "L"],
        format_func=lambda r: {"Any": "Any", "W": "Wins", "D": "Draws",
                               "L": "Losses"}[r],
        key=f"pg_result_{club_id}")

    g1, g2, g3 = st.columns(3)
    scope = g1.selectbox(
        f"{V.game.capitalize()} type", ["all", "home_and_away", "finals"],
        format_func=lambda s: {"all": f"All {V.games}",
                               "home_and_away": "Home and away",
                               "finals": V.postseason.capitalize()}[s],
        key=f"pg_scope_{club_id}")
    venue = g2.selectbox(
        V.venue.capitalize(), ["Any", *CH.venues_available(con)],
        key=f"pg_venue_{club_id}")
    order = g3.selectbox(
        "Sort by", ["date_desc", "date_asc", "margin_desc", "margin_asc",
                    "crowd_desc"],
        format_func=lambda o: {"date_desc": "Most recent",
                               "date_asc": "Oldest",
                               "margin_desc": "Biggest win",
                               "margin_asc": "Biggest loss",
                               "crowd_desc": "Biggest crowd"}[o],
        key=f"pg_order_{club_id}")

    matches = CH.search_matches(
        con, club_id=club_id,
        opponent_id=None if opponent == "Any" else opponent,
        season_from=lo, season_to=hi,
        venue=None if venue == "Any" else venue,
        result=None if result == "Any" else result,
        scope=scope, order=order, limit=2000)

    if not matches:
        st.info(f"No {V.games} for those filters.")
        return

    won = sum(m.result == "W" for m in matches)
    drew = sum(m.result == "D" for m in matches)
    st.caption(
        f"{len(matches):,} {V.games} · "
        f"{won}-{drew}-{len(matches) - won - drew} "
        f"(W-D-L)"
        + ("  ·  showing the first 2,000" if len(matches) == 2000 else ""))

    table = pd.DataFrame([{
        "Season": m.season, "Round": m.round, "Date": m.match_date,
        "Opponent": m.opponent_id.replace("_", " ").title(),
        "H/A": m.where.title(), "Result": m.result, "Score": m.score,
        "Margin": m.margin, "Venue": m.venue, "Crowd": m.attendance,
    } for m in matches])
    st.caption("Select a row to see that match's full detail.")
    components.clickable_match_table(
        table, matches, key=f"ce_pg_{club_id}", sport=sport, con=con,
        column_config={
            "Crowd": st.column_config.NumberColumn(format="%d"),
            "Margin": st.column_config.NumberColumn(format="%+d"),
        })
    st.download_button(
        "Download results CSV", table.to_csv(index=False),
        file_name=f"{club_id}_past_games.csv", mime="text/csv",
        key=f"pg_dl_{club_id}")

    with st.expander(f"{V.season.capitalize()}-by-{V.season} record"):
        by_season = CH.season_records(con, club_id, scope="home_and_away")
        seasons_table = pd.DataFrame([{
            "Season": r.season, "P": r.played, "W": r.wins, "D": r.draws,
            "L": r.losses, "For": r.points_for, "Against": r.points_against,
            "%": round(r.percentage, 1) if r.percentage is not None else None,
            "Pts": r.premiership_points,
        } for r in by_season])
        if seasons_table.empty:
            st.caption("No season records for this club.")
        else:
            # A row here is this club's season, so it opens that season:
            # who won it, how this club went and who led the scoring.
            st.caption(f"Select a {sport.vocab.season} for its overview.")
            components.clickable_season_table(
                seasons_table, seasons_table["Season"].tolist(), sport, con,
                key=f"ce_seasons_{club_id}",
                clubs=[games_name or club_name] * len(seasons_table))

    with st.expander("Longest streaks"):
        for kind in ("winning", "losing", "unbeaten"):
            runs = CH.streaks(con, club_id, kind=kind, limit=3)
            if not runs:
                continue
            best = ", ".join(
                f"{r.length} ({r.from_season}"
                + (f"–{r.to_season}" if r.spans_seasons else "") + ")"
                for r in runs)
            st.write(f"**{kind.title()}:** {best}")


@st.cache_data(show_spinner=False)
def _logo_map(_con, sport_key: str, folder_stamp) -> dict:
    """club_id -> logo file path, cached against the folder's contents.

    The cache key is a stamp over the folder rather than the connection, so
    dropping a new logo in refreshes it without restarting the app.
    """
    return {k: str(v) for k, v in
            CL.resolve(CL.clubs_from_db(_con),
                       CL.logo_dir(sport_key)).items()}


def _logo_stamp(sport_key: str) -> tuple:
    """Cheap fingerprint of the logo folder: names and modification times."""
    return tuple(sorted((p.name, p.stat().st_mtime_ns)
                        for p in CL.logo_files(CL.logo_dir(sport_key))))


def _logo_html(path: str, height: int = 72) -> str:
    """Inline the image as a data URI.

    Streamlit serves static files only from a configured static folder, and
    st.image does not render SVG. Inlining sidesteps both and keeps the
    logo a self-contained part of the page.
    """
    import base64
    import mimetypes

    data = pathlib.Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/svg+xml"
    encoded = base64.b64encode(data).decode("ascii")
    return (f"<img src='data:{mime};base64,{encoded}' "
            f"style='height:{height}px;width:auto;max-width:100%;'/>")


def _source_status(con, club_id: str) -> pd.DataFrame:
    return _read(con, """
        SELECT source_type AS Source,
               COALESCE(fetched_at, imported_at) AS Updated,
               revision_id AS Revision,
               source_url AS URL
        FROM club_source_snapshots
        WHERE club_id=?
        ORDER BY source_type
    """, (club_id,))


def club_explorer_page(sport, con: sqlite3.Connection) -> None:
    V = sport.vocab
    st.markdown(f"# {V.title_case('club')} Explorer")
    st.caption(
        f"Current-{V.club} metadata, all-time player registers, career "
        f"totals and {V.season}/{V.game} record leaderboards from locally "
        "cached source pages."
    )
    if not sport.has_club_explorer:
        st.info(f"{sport.vocab.title_case('club')} Explorer needs the "
                f"club-sources tables, which are not built for "
                f"{sport.label} yet.")
        return
    if not club_data_available(con):
        st.info(sport.club_data_hint or
                "Club data is not loaded for this sport.")
        return

    clubs = _read(con, """
        SELECT club_id, name, abbreviation, db_club_now
        FROM clubs WHERE active=1 ORDER BY name
    """)
    club_id = st.selectbox(
        sport.vocab.title_case("club"), clubs["club_id"].tolist(),
        format_func=lambda cid: clubs.loc[clubs.club_id == cid, "name"].iloc[0],
    )

    # Every club as a table as well as a picker: a row opens that club's
    # overview -- badge, span, titles and leading scorers -- without
    # changing which club the tabs below are showing.
    with st.expander(f"All {len(clubs)} {sport.vocab.clubs}"):
        listing = clubs.rename(columns={
            "name": sport.vocab.title_case("club"),
            "abbreviation": "Abbr"}).drop(columns=["club_id", "db_club_now"])
        st.caption(f"Select a {sport.vocab.club} for its overview.")
        components.clickable_club_table(
            listing,
            [row.db_club_now or row.name for row in clubs.itertuples()],
            sport, con, key="ce_all_clubs")

    club = clubs.loc[clubs.club_id == club_id].iloc[0]
    fields = _field_map(con, club_id)

    logos = _logo_map(con, sport.key, _logo_stamp(sport.key))
    logo = logos.get(club_id)
    if logo:
        badge, title = st.columns([1, 9])
        badge.markdown(_logo_html(logo), unsafe_allow_html=True)
        title.markdown(f"## {club['name']}")
    else:
        st.markdown(f"## {club['name']}")

    c1, c2, c3, c4 = st.columns(4)
    _headline(c1, fields, "nickname", "Nickname")
    _headline(c2, fields, "founded", "Founded")
    # The sport's own words: an NFL team has a home stadium and Super Bowls,
    # not a home ground and premierships.
    _headline(c3, fields, "ground", f"Home {sport.vocab.venue}")
    _headline(c4, fields, "premierships", sport.vocab.title_plural)

    tabs = st.tabs([
        "Overview", f"Past {V.games}", "Player totals",
        f"All-time {V.club} players", "Records", "Sources"
    ])

    with tabs[0]:
        # The preferred list named 'nickname', which fourteen of the
        # eighteen clubs do not use as a key -- their nickname is under
        # 'nickname_s'. Pull whichever spelling each club actually has.
        preferred = [
            "full_name", *CF.NICKNAME_KEYS, "founded", "colours",
            "competition", "chairperson", "ceo", "coach", "captain_s",
            "ground", "grounds", "training_ground", "premierships",
        ]
        seen = set()
        rows = []
        for key in preferred:
            value = fields.get(key)
            if value and key not in seen:
                seen.add(key)
                rows.append({"Field": key.replace("_", " ").title(),
                             "Value": CF.strip_footnotes(value)})
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={
                             "Field": st.column_config.TextColumn(width="small"),
                             "Value": st.column_config.TextColumn(width="large"),
                         })
        elif not fields:
            st.info("Wikipedia metadata has not been loaded for this club.")

        others = {k: v for k, v in sorted(fields.items()) if k not in seen}
        if others:
            with st.expander(f"All {len(fields)} scraped fields"):
                st.dataframe(
                    pd.DataFrame([
                        {"Field": k.replace("_", " ").title(),
                         "Value": CF.strip_footnotes(v)}
                        for k, v in others.items()]),
                    hide_index=True, width="stretch")

    with tabs[1]:
        # `db_club_now` is the club's name as the `games` rows spell it,
        # which is what a season overview has to be asked for -- the
        # display name and the games-table name are not always the same.
        _past_games_tab(con, sport, club_id, club["name"],
                        games_name=club.get("db_club_now") or club["name"])

    with tabs[2]:
        totals = _read(con, f"""
            SELECT {_stat_selection(con, 'club_player_totals')}
            FROM club_player_totals
            WHERE club_id=?
            ORDER BY games DESC, player_name
        """, (club_id,))
        totals = _drop_empty(totals, keep=("Player", "player_id"))
        if totals.empty:
            st.info("Player totals have not been loaded for this club.")
        else:
            table, ids = _split_links(totals)
            st.caption(f"{len(table):,} player-club career rows · select a "
                       "row to see that player's full career")
            components.clickable_player_table(
                table, ids, sport, con, key=f"ce_totals_{club_id}")
            st.download_button(
                "Download player totals CSV", table.to_csv(index=False),
                file_name=f"{club_id}_player_totals.csv", mime="text/csv",
            )

    with tabs[3]:
        register = _read(con, """
            SELECT cap_number AS Cap, jumper_number AS "Jumper #",
                   player_name AS Player, dob AS DOB, height_cm AS "Height cm",
                   weight_kg AS "Weight kg", games AS Games, wins AS Wins,
                   draws AS Draws, losses AS Losses, goals AS Goals,
                   seasons_text AS Seasons, debut_age_text AS "Debut age",
                   last_age_text AS "Last age", match_status AS Link,
                   player_id
            FROM club_player_register
            WHERE club_id=?
            ORDER BY games DESC, cap_number
        """, (club_id,))
        register = _drop_empty(register, keep=("Player", "player_id"))
        if register.empty:
            st.info("The all-time player list has not been loaded for "
                    "this club.")
        else:
            table, ids = _split_links(register)
            st.caption(f"{len(table):,} all-time club players · select a row "
                       "to see that player's full career")
            components.clickable_player_table(
                table, ids, sport, con, key=f"ce_register_{club_id}")
            st.download_button(
                "Download all-time players CSV", table.to_csv(index=False),
                file_name=f"{club_id}_all_time_players.csv", mime="text/csv",
            )

    with tabs[4]:
        options = con.execute(
            "SELECT DISTINCT scope, stat FROM club_player_records "
            "WHERE club_id=? ORDER BY scope, stat", (club_id,)
        ).fetchall()
        if not options:
            st.info("Season and Game Records HTML has not been loaded for this club.")
        else:
            scopes = sorted({scope for scope, _ in options})
            scope = st.radio("Record scope", scopes, horizontal=True)
            stats = [stat for row_scope, stat in options if row_scope == scope]
            stat = st.selectbox("Statistic", stats,
                                format_func=_label)
            records = _read(con, """
                SELECT source_rank AS Rank, player_name AS Player,
                       value AS Value, games AS Games, average AS Average,
                       season AS Season, round AS Round, opponent AS Opponent,
                       match_date AS Date, match_description AS Match,
                       source_team AS "Source team",
                       match_status AS Link, player_id
                FROM club_player_records
                WHERE club_id=? AND scope=? AND stat=?
                ORDER BY source_rank
            """, (club_id, scope, stat))
            table, ids = _split_links(records)
            st.caption("Select a row to see that player's full career.")
            components.clickable_player_table(
                table, ids, sport, con,
                key=f"ce_records_{club_id}_{scope}_{stat}")

    with tabs[5]:
        sources = _source_status(con, club_id)
        if sources.empty:
            st.info("No source snapshots have been loaded for this club.")
        else:
            st.dataframe(sources, hide_index=True, width="stretch",
                         column_config={"URL": st.column_config.LinkColumn("URL")})
        unresolved = _read(con, """
            SELECT 'Player totals' AS Dataset, match_status AS Status, COUNT(*) AS Rows
            FROM club_player_totals WHERE club_id=? GROUP BY match_status
            UNION ALL
            SELECT 'All-time list', match_status, COUNT(*)
            FROM club_player_register WHERE club_id=? GROUP BY match_status
            UNION ALL
            SELECT 'Records', match_status, COUNT(*)
            FROM club_player_records WHERE club_id=? GROUP BY match_status
        """, (club_id, club_id, club_id))
        if not unresolved.empty:
            st.markdown("### Link quality")
            st.dataframe(unresolved, hide_index=True, width="stretch")
