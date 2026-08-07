"""Shared Awards page for the normalized NBA/NFL Wikipedia layer."""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st


def awards_data_available(con: sqlite3.Connection) -> bool:
    try:
        return bool(con.execute("SELECT 1 FROM wiki_awards LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


def _catalogue(con):
    return pd.read_sql_query(
        """SELECT award_key, award_name, recipient_type,
                  COUNT(*) AS recipients, COUNT(player_id) AS linked,
                  MIN(season) AS season_from, MAX(season) AS season_to
             FROM wiki_awards
            GROUP BY award_key, award_name, recipient_type
            ORDER BY recipient_type DESC, award_name""", con)


def _roll(con, award_key):
    return pd.read_sql_query(
        """SELECT season AS Season, recipient AS Recipient, team AS Team,
                  position AS Position,
                  CASE WHEN player_id IS NOT NULL THEN 'linked'
                       ELSE match_status END AS Link,
                  player_id
             FROM wiki_awards WHERE award_key = ?
            ORDER BY season DESC, recipient""", con, params=(award_key,))


def _drop_empty(frame):
    keep = [column for column in frame.columns
            if column == "Season" or frame[column].fillna("").astype(str).str.strip().any()]
    return frame[keep].fillna("")


def awards_page(sport, con: sqlite3.Connection) -> None:
    st.markdown("# Awards")
    st.caption("Player and coach honours imported from the local Wikipedia "
               "Sports Scraper export. Unresolved names remain visible and "
               "are never guessed onto a player.")
    if not awards_data_available(con):
        st.info("Award data is not loaded. Run `python -m "
                "utils.shared.load_wiki_awards --sport " + sport.key
                + " --root <wiki-scrape-root>`.")
        return

    catalogue = _catalogue(con)
    kinds = list(dict.fromkeys(catalogue["recipient_type"].tolist()))
    kind = st.selectbox("Recipient type", kinds, key=sport.k("wiki_aw_kind"))
    choices = catalogue[catalogue["recipient_type"] == kind]
    labels = dict(zip(choices["award_key"], choices["award_name"]))
    award_key = st.selectbox("Award", list(labels), format_func=labels.get,
                             key=sport.k("wiki_aw_award"))
    summary = choices[choices["award_key"] == award_key].iloc[0]

    c1, c2, c3 = st.columns(3)
    c1.metric("Recipients", f"{int(summary.recipients):,}")
    span = (f"{int(summary.season_from)}–{int(summary.season_to)}"
            if pd.notna(summary.season_from) else "—")
    c2.metric("Seasons", span)
    c3.metric("Linked players", f"{int(summary.linked):,}")

    roll = _roll(con, award_key)
    f1, f2 = st.columns([1.5, 1])
    query = f1.text_input("Recipient contains", key=sport.k("wiki_aw_name", award_key))
    years = sorted(int(value) for value in roll["Season"].dropna().unique())
    selected = (f2.select_slider("Seasons", options=years,
                                  value=(years[0], years[-1]),
                                  key=sport.k("wiki_aw_years", award_key))
                if len(years) > 1 else None)
    shown = roll
    if query.strip():
        shown = shown[shown["Recipient"].str.contains(query.strip(), case=False,
                                                       na=False)]
    if selected:
        shown = shown[(shown["Season"] >= selected[0])
                      & (shown["Season"] <= selected[1])]
    table = _drop_empty(shown.drop(columns=["player_id"]))
    st.dataframe(table, hide_index=True, width="stretch")
    st.download_button("Download CSV", table.to_csv(index=False),
                       file_name=f"{sport.key}_{award_key}.csv", mime="text/csv")

    unresolved = shown[shown["player_id"].isna()]
    if not unresolved.empty and kind == "player":
        st.caption(f"{len(unresolved):,} row(s) are not linked to a unique "
                   "database player; they remain in the honour roll for audit.")
