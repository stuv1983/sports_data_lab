"""Streamlit page for browsing award winners.

The `awards` table already holds 1,810 rows across 38 awards -- Brownlow,
Norm Smith, Coleman, Rising Star, Leigh Matthews Trophy, the state-league
medals, every club best and fairest -- plus 906 All-Australian selections.
Until now the only way to reach any of it was through a grid constraint,
which answers "who has won one" and never "who won it in 1993".

Each award is shown as its own honour roll: one row per winner per season,
with the club and whatever the source recorded alongside (votes, games,
goals). A player name links back to the player database through
`person_links`, and only trusted links are followed -- an award row whose
person could not be resolved is still listed, because the award was still
won, but it carries no player profile.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

#: Link statuses the rest of the codebase treats as resolved.
TRUSTED = ("from_draft", "unique", "resolved")

#: Awards grouped for the picker, in the order a reader would look for
#: them. Anything in the table but not named here still appears, under
#: "Other awards" -- a new award arriving from the source should show up
#: without this file having to change first.
GROUPS = {
    "AFL/VFL major awards": [
        "brownlow-medal", "norm-smith-medal", "coleman", "rising-star",
        "aflpa-mvp", "aflca-champion", "aflca-best-young-player",
        "gary-ayres-award", "aflpa-best-first-year-player",
    ],
    "Selections": ["all-australian-squad"],
    "State leagues and juniors": [
        "magarey-medal", "sandover-medal", "liston-trophy", "morrish-medal",
        "larke-medal", "hunter-harrison-medal", "gardiner-medal",
        "geoff-christian-medal",
    ],
    "Draft": ["national_draft_pick_1"],
}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def awards_data_available(con: sqlite3.Connection) -> bool:
    have = _tables(con)
    if "awards" not in have:
        return False
    return bool(con.execute("SELECT 1 FROM awards LIMIT 1").fetchone())


def _catalogue(con: sqlite3.Connection) -> pd.DataFrame:
    """Every award in the table, with its coverage."""
    return pd.read_sql_query(
        """SELECT award_slug, award_name, award_category,
                  COUNT(*) AS winners,
                  MIN(season) AS season_from, MAX(season) AS season_to,
                  COUNT(DISTINCT season) AS seasons
           FROM awards GROUP BY award_slug
           ORDER BY award_category, award_name""", con)


def _honour_roll(con: sqlite3.Connection, slug: str) -> pd.DataFrame:
    """One row per winner per season, newest first."""
    return pd.read_sql_query(
        """SELECT a.season AS Season, a.player AS Player, a.club AS Club,
                  a.votes AS Votes, a.season_games AS "Games",
                  a.season_goals AS "Goals", a.note AS Note,
                  l.player_id AS player_id
           FROM awards a
           LEFT JOIN person_links l
             ON l.dg_person_id = a.dg_person_id
            AND l.match_status IN (?, ?, ?)
            AND l.player_id IS NOT NULL
           WHERE a.award_slug = ?
           ORDER BY a.season DESC, a.player""",
        con, params=(*TRUSTED, slug))


def _multiple_winners(con: sqlite3.Connection, slug: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT a.player AS Player, COUNT(*) AS Wins,
                  GROUP_CONCAT(a.season) AS Seasons
           FROM awards a WHERE a.award_slug = ?
           GROUP BY a.name_key HAVING COUNT(*) > 1
           ORDER BY COUNT(*) DESC, a.player""", con, params=(slug,))


def _all_australian(con: sqlite3.Connection, season: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT position AS Position, player AS Player, club AS Club,
                  times_aa AS "Times AA",
                  CASE WHEN is_captain THEN 'Captain'
                       WHEN is_vice_captain THEN 'Vice-captain'
                       ELSE '' END AS Role
           FROM all_australian WHERE season = ?
           ORDER BY is_captain DESC, is_vice_captain DESC, position, player""",
        con, params=(season,))


def _drop_empty(frame: pd.DataFrame) -> pd.DataFrame:
    """Hide columns the source never filled for this award.

    Draftguru records votes for the Brownlow and nothing for the Norm
    Smith. A column of blanks reads as missing data rather than as a field
    that does not apply to this award, so it is dropped instead.
    """
    keep = [c for c in frame.columns
            if c == "Season" or frame[c].notna().any()]
    return frame[keep]


def awards_page(sport, con: sqlite3.Connection) -> None:
    st.markdown("# Awards")
    st.caption(
        "Honour rolls for AFL/VFL, state-league and club awards, plus "
        "All-Australian selections and Hall of Fame inductees.")

    if not sport.has_awards_page:
        st.info(f"No award data is loaded for {sport.label}.")
        return
    if not awards_data_available(con):
        st.info("Award data is not loaded. Run `python -m afl.load_draftguru`, "
                "then `python -m afl.link_people`.")
        return

    have = _tables(con)
    catalogue = _catalogue(con)
    by_slug = {row.award_slug: row for row in catalogue.itertuples()}

    tab_names = ["Honour rolls", "All-Australian", "Club best and fairest"]
    if "hall_of_fame" in have:
        tab_names.append("Hall of Fame")
    if "team_selections" in have:
        tab_names.append("Teams of the Century")
    tab_names.append("All awards")
    tabs = st.tabs(tab_names)
    tab = dict(zip(tab_names, tabs))

    # ---------------------------------------------------- honour rolls
    with tab["Honour rolls"]:
        listed = {slug for slugs in GROUPS.values() for slug in slugs}
        groups = dict(GROUPS)
        others = [s for s in by_slug
                  if s not in listed
                  and by_slug[s].award_category != "club_best_and_fairest"]
        if others:
            groups["Other awards"] = sorted(others)

        group = st.selectbox("Group", list(groups), key="aw_group")
        options = [s for s in groups[group] if s in by_slug]
        if not options:
            st.info("None of these awards are loaded.")
        else:
            slug = st.selectbox(
                "Award", options,
                format_func=lambda s: by_slug[s].award_name, key="aw_slug")
            info = by_slug[slug]
            c1, c2, c3 = st.columns(3)
            c1.metric("Winners recorded", f"{info.winners:,}")
            c2.metric("Seasons", f"{info.season_from}–{info.season_to}")
            c3.metric("Distinct seasons", f"{info.seasons}")

            gap = info.season_to - info.season_from + 1 - info.seasons
            if gap > 0:
                st.caption(
                    f"{gap} season(s) in that range have no recorded winner. "
                    "The source table starts where it starts — an absent "
                    "season is missing from the source, not a year the "
                    "award went unawarded.")

            roll = _honour_roll(con, slug)
            unlinked = int(roll["player_id"].isna().sum())
            table = _drop_empty(roll.drop(columns=["player_id"]))
            st.dataframe(table, hide_index=True, width="stretch")
            st.download_button(
                "Download CSV", table.to_csv(index=False),
                file_name=f"{slug}.csv", mime="text/csv", key=f"dl_{slug}")
            if unlinked:
                st.caption(
                    f"{unlinked} of {len(roll)} rows could not be resolved to "
                    "a player in this database. They are listed because the "
                    "award was still won.")

            repeats = _multiple_winners(con, slug)
            if not repeats.empty:
                with st.expander(f"Multiple winners ({len(repeats)})"):
                    st.dataframe(repeats, hide_index=True, width="stretch")

    # --------------------------------------------------- all-australian
    with tab["All-Australian"]:
        if "all_australian" not in have:
            st.info("All-Australian data is not loaded.")
        else:
            seasons = [r[0] for r in con.execute(
                "SELECT DISTINCT season FROM all_australian "
                "ORDER BY season DESC")]
            season = st.selectbox("Season", seasons, key="aw_aa_season")
            st.dataframe(_all_australian(con, season), hide_index=True,
                         width="stretch")
            with st.expander("Most selections, all time"):
                st.dataframe(pd.read_sql_query(
                    """SELECT player AS Player, COUNT(*) AS Selections,
                              MIN(season) AS First, MAX(season) AS Last
                       FROM all_australian GROUP BY name_key
                       HAVING COUNT(*) > 1
                       ORDER BY COUNT(*) DESC, player LIMIT 100""", con),
                    hide_index=True, width="stretch")

    # ------------------------------------------------ best and fairest
    with tab["Club best and fairest"]:
        bnf = catalogue[catalogue.award_category == "club_best_and_fairest"]
        if bnf.empty:
            st.info("No club best-and-fairest awards are loaded.")
        else:
            slug = st.selectbox(
                "Club award", list(bnf.award_slug),
                format_func=lambda s: (f"{by_slug[s].award_name} "
                                       f"({s.replace('_', ' ').title()})"),
                key="aw_bnf")
            st.dataframe(
                _drop_empty(_honour_roll(con, slug).drop(
                    columns=["player_id"])),
                hide_index=True, width="stretch")

    # ------------------------------------------------------ hall of fame
    if "Hall of Fame" in tab:
        with tab["Hall of Fame"]:
            _hall_of_fame(con)

    # ----------------------------------------------- teams of the century
    if "Teams of the Century" in tab:
        with tab["Teams of the Century"]:
            _teams_of_the_century(con)

    # -------------------------------------------------------- catalogue
    with tab["All awards"]:
        st.caption("Every award in the database, with its coverage.")
        st.dataframe(
            catalogue.rename(columns={
                "award_name": "Award", "award_category": "Category",
                "winners": "Rows", "season_from": "From",
                "season_to": "To", "seasons": "Seasons",
            }).drop(columns=["award_slug"]),
            hide_index=True, width="stretch")


def _hall_of_fame(con: sqlite3.Connection) -> None:
    total, legends, linked = con.execute(
        "SELECT COUNT(*), SUM(is_legend), "
        "SUM(player_id IS NOT NULL) FROM hall_of_fame").fetchone()
    c1, c2, c3 = st.columns(3)
    c1.metric("Inductees", f"{total:,}")
    c2.metric("Legends", f"{legends or 0:,}")
    c3.metric("Linked to a player", f"{linked or 0:,}",
              help="Inductees include coaches, umpires and administrators, "
                   "who have no player record by definition.")

    only_legends = st.checkbox("Legends only", key="aw_hof_legend")
    where = " WHERE is_legend = 1" if only_legends else ""
    st.dataframe(pd.read_sql_query(
        f"""SELECT name AS Name, inducted_year AS Inducted,
                   CASE WHEN is_legend THEN 'Legend' ELSE '' END AS Status,
                   category AS Category, source_url AS Source
            FROM hall_of_fame{where}
            ORDER BY is_legend DESC, name""", con),
        hide_index=True, width="stretch",
        column_config={"Source": st.column_config.LinkColumn("Source")})


def _teams_of_the_century(con: sqlite3.Connection) -> None:
    teams = [r[0] for r in con.execute(
        "SELECT DISTINCT team_name FROM team_selections ORDER BY team_name")]
    if not teams:
        st.info("No team selections are loaded.")
        return
    team = st.selectbox("Team", teams, key="aw_toc")
    frame = pd.read_sql_query(
        """SELECT position AS Position, name AS Player, club AS Club,
                  note AS Note, player_id
           FROM team_selections WHERE team_name = ?
           ORDER BY sort_order, name""", con, params=(team,))
    unlinked = int(frame["player_id"].isna().sum())
    st.dataframe(_drop_empty(frame.drop(columns=["player_id"])),
                 hide_index=True, width="stretch")
    if unlinked:
        st.caption(f"{unlinked} of {len(frame)} selections could not be "
                   "resolved to a player in this database.")
