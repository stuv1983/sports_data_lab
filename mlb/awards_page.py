"""Streamlit page for browsing MLB award winners.

The Lahman export ships 12,667 award rows across 85 awards -- MVP and Cy
Young back to 1911 and 1956, Gold Gloves, Silver Sluggers, the postseason
MVPs, the Sporting News selections back to 1925 -- plus 6,426 Hall of Fame
ballot rows. Until now none of it had a page: the only way to reach an
award was through a grid constraint, which answers "who has won one" and
never "who won it in 1998".

Why this is not afl/awards_page.py
----------------------------------
That page is built for the Draftguru shape: `award_slug`, `award_name`,
per-award `votes`/`season_games`/`season_goals`, and a `person_links`
table resolving a scraped name to a player. Lahman has none of those. Its
awards table is (player_id, award, season, lgID, tie, notes), and
`player_id` *is* the Lahman playerID, so it joins straight to `players`
with no linkage layer and no trust boundary -- there is nothing to
resolve, and so nothing that can fail to resolve.

The two pages therefore share a shape rather than code, and app.py routes
to whichever the sport declares in `awards_page_module`.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

#: Awards grouped for the picker, in the order a reader looks for them.
#: Anything in the table but not named here appears under "Other awards",
#: so a new award in a later Lahman release shows up without this file
#: having to change first.
GROUPS = {
    "Major awards": [
        "Most Valuable Player", "Cy Young Award", "Rookie of the Year",
        "Gold Glove", "Silver Slugger", "Platinum Glove",
        "Reliever of the Year Award", "Reliever of the Year",
        "Comeback Player of the Year", "Outstanding DH Award",
        "Triple Crown", "Pitching Triple Crown", "Hank Aaron Award",
    ],
    "Postseason": [
        "World Series MVP", "ALCS MVP", "NLCS MVP", "All-Star Game MVP",
    ],
    "Selections": [
        "TSN All-Star", "Baseball Magazine All-Star",
        "All-MLB Team - First Team", "All-MLB Team - Second Team",
    ],
    "Sporting News": [
        "TSN Player of the Year", "TSN Pitcher of the Year",
        "TSN Major League Player of the Year", "TSN Rookie of the Year",
        "TSN Rookie Player of the Year", "TSN Rookie Pitcher of the Year",
        "TSN Fireman of the Year", "TSN Reliever of the Year",
        "TSN Comeback Player of the Year", "TSN Guide MVP",
    ],
    "Character and service": [
        "Roberto Clemente Award", "Lou Gehrig Memorial Award",
        "Branch Rickey Award", "Hutch Award", "Babe Ruth Award",
        "MLB Players Choice Man of the Year", "MLB Players Curt Flood Award",
    ],
    "Monthly and weekly": [
        "Player of the Week", "Player of the Month", "Pitcher of the Month",
        "Rookie of the Month", "Reliever of the Month",
    ],
}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def awards_data_available(con: sqlite3.Connection) -> bool:
    if "awards" not in _tables(con):
        return False
    return bool(con.execute("SELECT 1 FROM awards LIMIT 1").fetchone())


def _catalogue(con: sqlite3.Connection) -> pd.DataFrame:
    """Every award in the table, with its coverage."""
    return pd.read_sql_query(
        """SELECT award, COUNT(*) AS winners,
                  MIN(season) AS season_from, MAX(season) AS season_to,
                  COUNT(DISTINCT season) AS seasons
           FROM awards GROUP BY award ORDER BY award""", con)


def _honour_roll(con: sqlite3.Connection, award: str) -> pd.DataFrame:
    """One row per winner per season, newest first.

    LEFT JOIN, not JOIN: an award row whose player is somehow absent from
    `players` is still a win that happened, and dropping it would quietly
    shorten the honour roll rather than showing a gap.
    """
    return pd.read_sql_query(
        """SELECT a.season AS Season, p.player AS Player,
                  a.lgID AS League, a.tie AS Tie, a.notes AS Notes,
                  a.player_id AS player_id
           FROM awards a
           LEFT JOIN players p ON p.player_id = a.player_id
           WHERE a.award = ?
           ORDER BY a.season DESC, p.player""",
        con, params=(award,))


def _multiple_winners(con: sqlite3.Connection, award: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT p.player AS Player, COUNT(*) AS Wins,
                  GROUP_CONCAT(a.season) AS Seasons
           FROM awards a JOIN players p ON p.player_id = a.player_id
           WHERE a.award = ?
           GROUP BY a.player_id HAVING COUNT(*) > 1
           ORDER BY COUNT(*) DESC, p.player""", con, params=(award,))


def _hall_of_fame(con: sqlite3.Connection) -> pd.DataFrame:
    """Inducted members only.

    `hall_of_fame` is a ballot table, not a member list: most of its 6,426
    rows are a player appearing on a ballot and not getting in, sometimes
    for fifteen years running. Filtering on inducted='Y' is what turns it
    into the honour roll people expect.
    """
    return pd.read_sql_query(
        """SELECT h.season AS Inducted, p.player AS Player,
                  h.category AS Category, h.votedBy AS "Voted by",
                  h.votes AS Votes, h.ballots AS Ballots
           FROM hall_of_fame h
           LEFT JOIN players p ON p.player_id = h.player_id
           WHERE UPPER(TRIM(h.inducted)) = 'Y'
           ORDER BY h.season DESC, p.player""", con)


def _drop_empty(frame: pd.DataFrame) -> pd.DataFrame:
    """Hide columns this award never fills, and blank the nulls in the rest.

    Lahman records `tie` for a handful of awards and `notes` (the fielding
    position) only for Gold Gloves and Silver Sluggers. A column of blanks
    reads as missing data rather than as a field that does not apply, so an
    entirely empty one is dropped.

    The surviving columns still need the fill: `tie` is 'Y' on 659 rows and
    NULL on 12,008, and Streamlit prints a None in an object column as the
    literal word "None" -- a table reading "None" all the way down the Tie
    column says the opposite of what the blank means.
    """
    keep = [c for c in frame.columns
            if c == "Season" or frame[c].notna().any()]
    out = frame[keep].copy()
    for column in out.columns:
        if out[column].dtype == object:
            out[column] = out[column].fillna("")
    return out


def awards_page(sport, con: sqlite3.Connection) -> None:
    import components

    st.markdown("# Awards")
    st.caption("MVP, Cy Young, Gold Gloves, the postseason MVPs and the "
               "Sporting News selections, plus Hall of Fame inductions.")

    if not awards_data_available(con):
        st.info("Award data is not loaded. Run `python -m mlb.build_mlb_db`.")
        return

    catalogue = _catalogue(con)
    known = set(catalogue["award"])
    grouped = {name: [a for a in awards if a in known]
               for name, awards in GROUPS.items()}
    listed = {a for awards in grouped.values() for a in awards}
    other = sorted(known - listed)
    if other:
        grouped["Other awards"] = other
    grouped = {name: awards for name, awards in grouped.items() if awards}

    group = st.selectbox("Category", list(grouped), key=sport.k("mlb_aw_group"))
    award = st.selectbox("Award", grouped[group], key=sport.k("mlb_aw_award"))

    row = catalogue[catalogue["award"] == award].iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("Winners recorded", f"{int(row.winners):,}")
    c2.metric("Seasons", f"{int(row.season_from)}–{int(row.season_to)}")
    c3.metric("Distinct seasons", f"{int(row.seasons):,}")

    roll = _honour_roll(con, award)
    if roll.empty:
        st.info("No rows for that award.")
        return

    player_ids = roll["player_id"].tolist()
    table = _drop_empty(roll.drop(columns=["player_id"]))
    st.caption("Select a row to see that player's full career.")
    components.clickable_player_table(
        table, player_ids, sport, con, key=sport.k("mlb_aw_roll"))
    st.download_button(
        "Download CSV", table.to_csv(index=False),
        file_name=f"mlb_{award.lower().replace(' ', '_')}.csv",
        mime="text/csv")

    repeats = _multiple_winners(con, award)
    if not repeats.empty:
        with st.expander(f"Multiple winners ({len(repeats):,})"):
            st.dataframe(repeats, hide_index=True, width="stretch")

    if "hall_of_fame" in _tables(con):
        st.markdown("### Hall of Fame")
        hof = _hall_of_fame(con)
        if hof.empty:
            st.caption("No inductions recorded.")
        else:
            st.caption(f"{len(hof):,} inducted members. The source table is "
                       "a ballot record, so players who appeared on a ballot "
                       "without being elected are not listed here.")
            st.dataframe(hof, hide_index=True, width="stretch")
