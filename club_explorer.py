"""Streamlit Club Explorer for the optional 18-club data layer."""
from __future__ import annotations

import json
import sqlite3

import pandas as pd
import streamlit as st

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
    st.markdown("# Club Explorer")
    st.caption(
        "Current-club metadata, all-time player registers, career totals and "
        "season/game record leaderboards from locally cached source pages."
    )
    if sport.key != "afl":
        st.info("Club Explorer is currently available for AFL only.")
        return
    if not club_data_available(con):
        st.info(
            "Club data is not loaded. Run `python utils/fetch_club_sources.py "
            "--report`, then `python utils/load_club_sources.py --report "
            "--details`."
        )
        return

    clubs = _read(con, """
        SELECT club_id, name, abbreviation
        FROM clubs WHERE active=1 ORDER BY name
    """)
    club_id = st.selectbox(
        "Club", clubs["club_id"].tolist(),
        format_func=lambda cid: clubs.loc[clubs.club_id == cid, "name"].iloc[0],
    )
    club = clubs.loc[clubs.club_id == club_id].iloc[0]
    fields = _field_map(con, club_id)

    st.markdown(f"## {club['name']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nickname", _first(fields, "nickname", "nicknames"))
    c2.metric("Founded", _first(fields, "founded", "established"))
    c3.metric("Home ground", _first(fields, "ground", "grounds"))
    c4.metric("Premierships", _first(fields, "premierships"))

    tabs = st.tabs([
        "Overview", "Player totals", "All-time players", "Records", "Sources"
    ])

    with tabs[0]:
        preferred = [
            "full_name", "nickname", "founded", "colours", "competition",
            "chairperson", "ceo", "coach", "captain_s", "ground",
            "training_ground", "premierships",
        ]
        rows = []
        for key in preferred:
            if fields.get(key):
                rows.append({"Field": key.replace("_", " ").title(),
                             "Value": fields[key]})
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        elif fields:
            st.dataframe(
                pd.DataFrame([{"Field": k.replace("_", " ").title(), "Value": v}
                              for k, v in sorted(fields.items())]),
                hide_index=True, width="stretch",
            )
        else:
            st.info("Wikipedia metadata has not been loaded for this club.")

    with tabs[1]:
        totals = _read(con, """
            SELECT player_name AS Player, games AS Games, goals AS Goals,
                   disposals AS Disposals, kicks AS Kicks, marks AS Marks,
                   handballs AS Handballs, tackles AS Tackles,
                   brownlow AS "Brownlow votes", match_status AS Link
            FROM club_player_totals
            WHERE club_id=?
            ORDER BY games DESC, player_name
        """, (club_id,))
        if totals.empty:
            st.info("Player Totals HTML has not been loaded for this club.")
        else:
            st.caption(f"{len(totals):,} player-club career rows")
            st.dataframe(totals, hide_index=True, width="stretch")
            st.download_button(
                "Download player totals CSV", totals.to_csv(index=False),
                file_name=f"{club_id}_player_totals.csv", mime="text/csv",
            )

    with tabs[2]:
        register = _read(con, """
            SELECT cap_number AS Cap, jumper_number AS "Jumper #",
                   player_name AS Player, dob AS DOB, height_cm AS "Height cm",
                   weight_kg AS "Weight kg", games AS Games, wins AS Wins,
                   draws AS Draws, losses AS Losses, goals AS Goals,
                   seasons_text AS Seasons, debut_age_text AS "Debut age",
                   last_age_text AS "Last age", match_status AS Link
            FROM club_player_register
            WHERE club_id=?
            ORDER BY games DESC, cap_number
        """, (club_id,))
        if register.empty:
            st.info("All-Time Player List HTML has not been loaded for this club.")
        else:
            st.caption(f"{len(register):,} all-time club players")
            st.dataframe(register, hide_index=True, width="stretch")
            st.download_button(
                "Download all-time players CSV", register.to_csv(index=False),
                file_name=f"{club_id}_all_time_players.csv", mime="text/csv",
            )

    with tabs[3]:
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
                                format_func=lambda value: value.replace("_", " ").title())
            records = _read(con, """
                SELECT source_rank AS Rank, player_name AS Player,
                       value AS Value, games AS Games, average AS Average,
                       season AS Season, round AS Round, opponent AS Opponent,
                       match_date AS Date, match_description AS Match,
                       source_team AS "Source team",
                       match_status AS Link
                FROM club_player_records
                WHERE club_id=? AND scope=? AND stat=?
                ORDER BY source_rank
            """, (club_id, scope, stat))
            st.dataframe(records, hide_index=True, width="stretch")

    with tabs[4]:
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
