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

import components

#: Link statuses the rest of the codebase treats as resolved.
TRUSTED = ("from_draft", "unique", "resolved")

ANY = "Any"

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
    catalogue = pd.read_sql_query(
        """SELECT award_slug, award_name, award_category,
                  COUNT(*) AS winners,
                  MIN(season) AS season_from, MAX(season) AS season_to,
                  COUNT(DISTINCT season) AS seasons
           FROM awards GROUP BY award_slug
           ORDER BY award_category, award_name""", con)
    if "brownlow_results" in _tables(con):
        summary = con.execute(
            """SELECT COUNT(*), MIN(season), MAX(season), COUNT(DISTINCT season)
                 FROM brownlow_results
                WHERE winner=1 AND match_status IN ('unique','resolved')"""
        ).fetchone()
        if summary and summary[0]:
            mask = catalogue["award_slug"] == "brownlow-medal"
            if mask.any():
                catalogue.loc[mask, [
                    "winners", "season_from", "season_to", "seasons"
                ]] = summary
            else:
                catalogue = pd.concat([catalogue, pd.DataFrame([{
                    "award_slug": "brownlow-medal",
                    "award_name": "Brownlow Medal",
                    "award_category": "award",
                    "winners": summary[0], "season_from": summary[1],
                    "season_to": summary[2], "seasons": summary[3],
                }])], ignore_index=True)
    return catalogue


def _honour_roll(con: sqlite3.Connection, slug: str) -> pd.DataFrame:
    """One row per winner per season, newest first."""
    if slug == "brownlow-medal" and "brownlow_results" in _tables(con):
        return pd.read_sql_query(
            """SELECT b.season AS Season, b.player AS Player,
                      COALESCE((
                          SELECT GROUP_CONCAT(club_hist, ' / ') FROM (
                              SELECT DISTINCT g.club_hist
                                FROM games g
                               WHERE g.player_id=b.player_id
                                 AND g.season=b.season
                               ORDER BY g.club_hist
                          )
                      ), b.clubs) AS Club,
                      b.votes AS Votes, b.games AS Games,
                      NULL AS Goals,
                      CASE WHEN b.ineligible=1 THEN 'Ineligible' END AS Note,
                      b.player_id AS player_id
                 FROM brownlow_results b
                WHERE b.winner=1
                  AND b.match_status IN ('unique','resolved')
                ORDER BY b.season DESC, b.player""", con)
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


def _clubs_for(con: sqlite3.Connection, slug: str) -> list[str]:
    """Clubs this award has actually been won by, for the filter.

    Read from the award's own rows rather than from the club list: the
    source spells a club as it was spelled at the time ('Brisbane', 'South
    Melbourne'), and offering the current eighteen would filter half the
    honour roll to nothing.
    """
    if slug == "brownlow-medal" and "brownlow_results" in _tables(con):
        roll = _honour_roll(con, slug)
        return sorted({str(club).strip() for club in roll["Club"].dropna()
                       if str(club).strip()})
    return [row[0] for row in con.execute(
        "SELECT DISTINCT club FROM awards "
        "WHERE award_slug = ? AND club IS NOT NULL AND TRIM(club) != '' "
        "ORDER BY club", (slug,))]


def _filter_roll(roll: pd.DataFrame, club: str, seasons, name: str
                 ) -> pd.DataFrame:
    """Apply the honour-roll filters, each one optional."""
    out = roll
    if club and club != ANY and "Club" in out.columns:
        out = out[out["Club"].astype(str).str.strip() == club]
    if seasons and "Season" in out.columns:
        lo, hi = seasons
        out = out[(out["Season"] >= lo) & (out["Season"] <= hi)]
    if name.strip() and "Player" in out.columns:
        out = out[out["Player"].astype(str).str.contains(
            name.strip(), case=False, na=False)]
    return out


def _multiple_winners(con: sqlite3.Connection, slug: str) -> pd.DataFrame:
    if slug == "brownlow-medal" and "brownlow_results" in _tables(con):
        return pd.read_sql_query(
            """SELECT player AS Player, COUNT(*) AS Wins,
                      GROUP_CONCAT(season) AS Seasons,
                      player_id
                 FROM brownlow_results
                WHERE winner=1 AND match_status IN ('unique','resolved')
                GROUP BY player_id, player HAVING COUNT(*) > 1
                ORDER BY COUNT(*) DESC, player""", con)
    return pd.read_sql_query(
        """SELECT a.player AS Player, COUNT(*) AS Wins,
                  GROUP_CONCAT(a.season) AS Seasons,
                  MAX(l.player_id) AS player_id
           FROM awards a
           LEFT JOIN person_links l ON l.dg_person_id = a.dg_person_id
             AND l.match_status IN ('from_draft','unique','resolved')
           WHERE a.award_slug = ?
           GROUP BY a.name_key HAVING COUNT(*) > 1
           ORDER BY COUNT(*) DESC, a.player""", con, params=(slug,))


def _all_australian(con: sqlite3.Connection, season: int) -> pd.DataFrame:
    """One season's side, with the player link the roll uses."""
    return pd.read_sql_query(
        """SELECT a.position AS Position, a.player AS Player, a.club AS Club,
                  a.times_aa AS "Times AA",
                  CASE WHEN a.is_captain THEN 'Captain'
                       WHEN a.is_vice_captain THEN 'Vice-captain'
                       ELSE '' END AS Role,
                  l.player_id AS player_id
           FROM all_australian a
           LEFT JOIN person_links l
             ON l.dg_person_id = a.dg_person_id
            AND l.match_status IN (?, ?, ?)
            AND l.player_id IS NOT NULL
           WHERE a.season = ?
           ORDER BY a.is_captain DESC, a.is_vice_captain DESC,
                    a.position, a.player""",
        con, params=(*TRUSTED, season))


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

    # The strapline names this page's actual contents, which are the AFL's.
    # It used to be printed for every sport, so the NBA and the NFL -- which
    # have no award data at all and fall through to this page because they
    # declare no module of their own -- announced All-Australian selections
    # before saying they had nothing.
    if not sport.has_awards_page:
        st.info(f"No award data is loaded for {sport.label}. "
                f"{sport.vocab.title_case('club')} honours for this sport "
                "have not been imported yet.")
        return
    st.caption(
        "Honour rolls for AFL/VFL, state-league and club awards, plus "
        "All-Australian selections and Hall of Fame inductees.")
    have = _tables(con)
    if not awards_data_available(con):
        if "brownlow_results" in have:
            _brownlow_voting(sport, con)
        else:
            st.info("Award data is not loaded. Run `python -m afl.load_draftguru`, "
                    "then `python -m afl.link_people`.")
        return

    catalogue = _catalogue(con)
    by_slug = {row.award_slug: row for row in catalogue.itertuples()}

    tab_names = ["Honour rolls", "All-Australian", "Club best and fairest"]
    if "brownlow_results" in have:
        tab_names.insert(1, "Brownlow voting")
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
            if gap > 0 and slug == "brownlow-medal":
                st.caption(
                    f"{gap} season(s) in that range have no winner. The "
                    "Brownlow Medal was not awarded from 1942 to 1945.")
            elif gap > 0:
                st.caption(
                    f"{gap} season(s) in that range have no recorded winner. "
                    "The source table starts where it starts — an absent "
                    "season is missing from the source, not a year the "
                    "award went unawarded.")
            if slug == "brownlow-medal":
                st.caption(
                    "Voting format: one best-on-ground vote per match from "
                    "1924 to 1930; 3-2-1 from 1931, except 1976 and 1977 when "
                    "two field umpires voted independently.")

            roll = _honour_roll(con, slug)

            # Search within the roll. A roll is a century of winners, and
            # "who won it for Carlton", "who won it in the eighties" and
            # "did this player ever win it" were all questions the page
            # could show the answer to but not let anyone ask.
            s1, s2, s3 = st.columns([1.2, 1.6, 1.2])
            clubs = _clubs_for(con, slug)
            club = s1.selectbox(f"{sport.vocab.title_case('club')}",
                                [ANY, *clubs], key=f"aw_club_{slug}",
                                disabled=not clubs,
                                help=None if clubs else
                                "The source records no club for this award.")
            all_seasons = sorted({int(s) for s in roll["Season"].dropna()})
            if len(all_seasons) > 1:
                seasons = s2.select_slider(
                    "Seasons", options=all_seasons,
                    value=(all_seasons[0], all_seasons[-1]),
                    key=f"aw_years_{slug}")
            else:
                seasons = None
            name = s3.text_input("Player contains", key=f"aw_name_{slug}",
                                 placeholder="surname…")

            shown = _filter_roll(roll, club, seasons, name)
            if shown.empty:
                st.info("No winners match those filters.")
            else:
                if len(shown) != len(roll):
                    st.caption(f"{len(shown)} of {len(roll)} rows shown.")
                unlinked = int(shown["player_id"].isna().sum())
                player_ids = shown["player_id"].tolist()
                table = _drop_empty(shown.drop(columns=["player_id"]))
                st.caption("Select a row to see that player's full career.")
                components.clickable_player_table(
                    table, player_ids, sport, con, key=f"aw_roll_{slug}")
                st.download_button(
                    "Download CSV", table.to_csv(index=False),
                    file_name=f"{slug}.csv", mime="text/csv",
                    key=f"dl_{slug}")
                if unlinked:
                    st.caption(
                        f"{unlinked} of {len(shown)} rows could not be "
                        "resolved to a player in this database. They are "
                        "listed because the award was still won.")

            repeats = _multiple_winners(con, slug)
            if not repeats.empty:
                with st.expander(f"Multiple winners ({len(repeats)})"):
                    components.clickable_player_table(
                        repeats.drop(columns=["player_id"]),
                        repeats["player_id"].tolist(), sport, con,
                        key=f"aw_repeat_{slug}")

    # --------------------------------------------------- Brownlow voting
    if "Brownlow voting" in tab:
        with tab["Brownlow voting"]:
            _brownlow_voting(sport, con)

    # --------------------------------------------------- all-australian
    with tab["All-Australian"]:
        if "all_australian" not in have:
            st.info("All-Australian data is not loaded.")
        else:
            seasons = [r[0] for r in con.execute(
                "SELECT DISTINCT season FROM all_australian "
                "ORDER BY season DESC")]
            a1, a2 = st.columns([1, 2])
            season = a1.selectbox("Season", seasons, key="aw_aa_season")
            club_filter = a2.selectbox(
                "Club", [ANY, *[r[0] for r in con.execute(
                    "SELECT DISTINCT club FROM all_australian "
                    "WHERE club IS NOT NULL AND TRIM(club) != '' "
                    "ORDER BY club")]],
                key="aw_aa_club")
            side = _all_australian(con, season)
            if club_filter != ANY:
                side = side[side["Club"].astype(str).str.strip()
                            == club_filter]
            if side.empty:
                st.info("No selections match those filters.")
            else:
                player_ids = side["player_id"].tolist()
                st.caption("Select a row to see that player's full career.")
                components.clickable_player_table(
                    side.drop(columns=["player_id"]), player_ids, sport, con,
                    key=f"aw_aa_{season}")
            with st.expander("Most selections, all time"):
                most = pd.read_sql_query(
                    """SELECT a.player AS Player, COUNT(*) AS Selections,
                              MIN(a.season) AS First, MAX(a.season) AS Last,
                              MAX(l.player_id) AS player_id
                       FROM all_australian a
                       LEFT JOIN person_links l
                         ON l.dg_person_id = a.dg_person_id
                        AND l.match_status IN ('from_draft','unique','resolved')
                       GROUP BY a.name_key
                       HAVING COUNT(*) > 1
                       ORDER BY COUNT(*) DESC, a.player LIMIT 100""", con)
                components.clickable_player_table(
                    most.drop(columns=["player_id"]),
                    most["player_id"].tolist(), sport, con,
                    key="aw_aa_most")

    # ------------------------------------------------ best and fairest
    with tab["Club best and fairest"]:
        bnf = catalogue[catalogue.award_category == "club_best_and_fairest"]
        if bnf.empty:
            st.info("No club best-and-fairest awards are loaded.")
        else:
            b1, b2, b3 = st.columns([1.6, 1.6, 1.2])
            slug = b1.selectbox(
                "Club award", list(bnf.award_slug),
                format_func=lambda s: (f"{by_slug[s].award_name} "
                                       f"({s.replace('_', ' ').title()})"),
                key="aw_bnf")
            roll = _honour_roll(con, slug)
            years = sorted({int(s) for s in roll["Season"].dropna()})
            span = (b2.select_slider(
                "Seasons", options=years, value=(years[0], years[-1]),
                key=f"aw_bnf_years_{slug}") if len(years) > 1 else None)
            name = b3.text_input("Player contains", key=f"aw_bnf_name_{slug}",
                                 placeholder="surname…")
            shown = _filter_roll(roll, ANY, span, name)
            if shown.empty:
                st.info("No winners match those filters.")
            else:
                st.caption("Select a row to see that player's full career.")
                components.clickable_player_table(
                    _drop_empty(shown.drop(columns=["player_id"])),
                    shown["player_id"].tolist(), sport, con,
                    key=f"aw_bnf_roll_{slug}")

    # ------------------------------------------------------ hall of fame
    if "Hall of Fame" in tab:
        with tab["Hall of Fame"]:
            _hall_of_fame(sport, con)

    # ----------------------------------------------- teams of the century
    if "Teams of the Century" in tab:
        with tab["Teams of the Century"]:
            _teams_of_the_century(sport, con)

    # -------------------------------------------------------- catalogue
    with tab["All awards"]:
        st.caption("Every award in the database, with its coverage.")
        catalogue_table = catalogue.rename(columns={
                "award_name": "Award", "award_category": "Category",
                "winners": "Rows", "season_from": "From",
                "season_to": "To", "seasons": "Distinct seasons",
            }).drop(columns=["award_slug"])
        components.clickable_season_table(
            catalogue_table, [None] * len(catalogue_table), sport, con,
            key="aw_catalogue")


def _brownlow_voting(sport, con: sqlite3.Connection) -> None:
    """Full season voting, including non-winners and ineligible players."""
    seasons = [row[0] for row in con.execute(
        "SELECT DISTINCT season FROM brownlow_results "
        "WHERE match_status IN ('unique','resolved') ORDER BY season DESC")]
    if not seasons:
        st.info("No linked Brownlow voting results are loaded.")
        return

    st.caption(
        "Complete AFL Tables voting results. Finish is the official eligible "
        "rank; suspended players retain their vote rank but have no finish.")
    f1, f2, f3 = st.columns([1, 1, 1.4])
    season = f1.selectbox("Season", seasons, key="bm_season")
    top = f2.number_input("Show top", min_value=3, max_value=300,
                          value=30, step=5, key="bm_top")
    name = f3.text_input("Player contains", placeholder="surnameâ€¦",
                         key="bm_name")

    frame = pd.read_sql_query(
        """SELECT player AS Player, clubs AS Club, votes AS Votes,
                  vote_rank AS "Vote rank", eligible_rank AS Finish,
                  CASE WHEN winner=1 THEN 'Winner'
                       WHEN ineligible=1 THEN 'Ineligible' ELSE '' END AS Status,
                  games AS Games, three_vote_games AS "3 votes",
                  two_vote_games AS "2 votes", one_vote_games AS "1 vote",
                  player_url AS Source, player_id
             FROM brownlow_results
            WHERE season=? AND match_status IN ('unique','resolved')
            ORDER BY votes DESC, player""",
        con, params=(season,))
    full = frame
    if name.strip():
        frame = frame[frame["Player"].str.contains(
            name.strip(), case=False, na=False)]
    else:
        frame = frame.head(int(top))

    winners = full[full["Status"] == "Winner"]["Player"].tolist()
    m1, m2, m3 = st.columns(3)
    m1.metric("Winner" if len(winners) == 1 else "Winners",
              ", ".join(winners) or "â€”")
    m2.metric("Players polling votes", f"{len(full):,}")
    m3.metric("Winning votes", int(full.loc[
        full["Status"] == "Winner", "Votes"].max()) if winners else "â€”")

    if frame.empty:
        st.info("No players match that search.")
    else:
        player_ids = frame["player_id"].tolist()
        table = _drop_empty(frame.drop(columns=["player_id"]))
        st.caption("Select a row to see that player's full career.")
        components.clickable_player_table(
            table, player_ids, sport, con, key=f"bm_results_{season}",
            column_config={"Source": st.column_config.LinkColumn("Source")})
        st.download_button(
            "Download season CSV", table.to_csv(index=False),
            file_name=f"brownlow-{season}.csv", mime="text/csv",
            key=f"bm_download_{season}")

    rounds = pd.read_sql_query(
        """SELECT round_number AS Round,
                  SUM(COALESCE(votes,0)) AS "Votes awarded",
                  SUM(COALESCE(votes,0)>0) AS "Players polling",
                  SUM(votes=3) AS "3 votes", SUM(votes=2) AS "2 votes",
                  SUM(votes=1) AS "1 vote"
             FROM brownlow_round_votes
            WHERE season=? AND played=1
            GROUP BY round_number ORDER BY round_number""",
        con, params=(season,))
    if not rounds.empty:
        st.markdown("#### Round by round")
        st.caption("Select a round to see every result and its Brownlow pollers.")
        components.clickable_round_table(
            rounds, [season] * len(rounds), sport, con,
            key=f"bm_rounds_{season}")

    with st.expander("Career voting leaders"):
        leaders = pd.read_sql_query(
            """SELECT player AS Player, SUM(votes) AS Votes,
                      COUNT(*) AS "Seasons polling",
                      MIN(eligible_rank) AS "Best finish",
                      SUM(eligible_rank <= 3) AS "Top 3",
                      SUM(eligible_rank <= 10) AS "Top 10",
                      MAX(player_id) AS player_id
                 FROM brownlow_results
                WHERE match_status IN ('unique','resolved')
                GROUP BY player_id, player
                ORDER BY SUM(votes) DESC, player LIMIT 100""", con)
        components.clickable_player_table(
            leaders.drop(columns=["player_id"]), leaders["player_id"].tolist(),
            sport, con, key="bm_career_leaders")


def _hall_of_fame(sport, con: sqlite3.Connection) -> None:
    total, legends, linked = con.execute(
        "SELECT COUNT(*), SUM(is_legend), "
        "SUM(player_id IS NOT NULL) FROM hall_of_fame").fetchone()
    c1, c2, c3 = st.columns(3)
    c1.metric("Inductees", f"{total:,}")
    c2.metric("Legends", f"{legends or 0:,}")
    c3.metric("Linked to a player", f"{linked or 0:,}",
              help="Inductees include coaches, umpires and administrators, "
                   "who have no player record by definition.")

    h1, h2, h3 = st.columns([1, 1.6, 1.2])
    only_legends = h1.checkbox("Legends only", key="aw_hof_legend")
    clubs = [r[0] for r in con.execute(
        "SELECT DISTINCT club FROM hall_of_fame "
        "WHERE club IS NOT NULL AND TRIM(club) != '' ORDER BY club")]
    club = h2.selectbox(f"{sport.vocab.title_case('club')}", [ANY, *clubs],
                        key="aw_hof_club", disabled=not clubs)
    name = h3.text_input("Name contains", key="aw_hof_name",
                         placeholder="surname…")

    where, params = [], []
    if only_legends:
        where.append("is_legend = 1")
    if club != ANY:
        where.append("club = ?")
        params.append(club)
    if name.strip():
        where.append("name LIKE ?")
        params.append(f"%{name.strip()}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    frame = pd.read_sql_query(
        f"""SELECT name AS Name, inducted_year AS Inducted,
                   CASE WHEN is_legend THEN 'Legend' ELSE '' END AS Status,
                   club AS Club, category AS Category, source_url AS Source,
                   player_id
            FROM hall_of_fame{clause}
            ORDER BY is_legend DESC, name""", con, params=tuple(params))
    if frame.empty:
        st.info("No inductees match those filters.")
        return
    st.caption("Select a row to see that player's full career.")
    components.clickable_player_table(
        _drop_empty(frame.drop(columns=["player_id"])),
        frame["player_id"].tolist(), sport, con, key="aw_hof_table",
        column_config={"Source": st.column_config.LinkColumn("Source")})


def _teams_of_the_century(sport, con: sqlite3.Connection) -> None:
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
    st.caption("Select a row to see that player's full career.")
    components.clickable_player_table(
        _drop_empty(frame.drop(columns=["player_id"])),
        frame["player_id"].tolist(), sport, con, key=f"aw_toc_{team}")
    if unlinked:
        st.caption(f"{unlinked} of {len(frame)} selections could not be "
                   "resolved to a player in this database.")
