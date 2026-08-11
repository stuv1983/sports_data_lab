"""Streamlit page for searching past results across every club.

The Club Explorer's Past games tab answers "how has this club gone?" and
is scoped to one club by construction. This page answers the other
question -- "what happened in this match / at this ground / that year?" --
so both clubs, the ground and the date are filters rather than fixed
context, and a search with no club at all is a valid search.

Reads club_history, which reads the all-games source rows: one row per club
per match, 1897 to the present, with result, margin, venue, crowd and the
score at every quarter break.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pandas as pd
import streamlit as st

import components
from . import club_history as CH

ANY = "Any"


#: club_id -> display name where title-casing gets it wrong.
_NAME_FIXES = {"gws": "GWS"}


def _label(club_id: str) -> str:
    """Display name for a source_club_id.

    The AFL's ids are slugs ('brisbane_bears') and need title-casing. The
    MLB's are already display names, because Retrosheet's team codes are
    opaque and load_retrosheet.py resolves them to the club's name in that
    season. Title-casing those damages them -- "Brooklyn Ward's Wonders"
    became "Ward'S Wonders" and "Angels of Anaheim" became "Of Anaheim" --
    so only a slug is transformed and anything already in display form is
    passed through untouched.
    """
    if club_id in _NAME_FIXES:
        return _NAME_FIXES[club_id]
    if club_id.islower():
        return club_id.replace("_", " ").title()
    return club_id


def _match_table(matches, two_sided: bool) -> pd.DataFrame:
    """One row per match.

    With no club filter the rows are already one per match and neither side
    is 'the' club, so the columns name both teams and the score reads
    first-named side first. With a club filter every row is that club's
    view and the result and margin belong to it.

    Every row must carry the same keys. An earlier version named the
    columns per row -- 'Home'/'Away' for a home-and-away match, but
    'Team'/'Opponent' for a final, which has no home side -- and pandas
    duly produced all four columns with half of each one empty.
    """
    if two_sided:
        return pd.DataFrame([{
            "Season": m.season, "Round": m.round or "", "Date": m.match_date,
            "Home": _label(m.club_id), "Away": _label(m.opponent_id),
            "Score": m.score, "Margin": abs(m.margin),
            "Venue": m.venue, "Crowd": m.attendance,
            "Final": "Yes" if m.is_final else "",
        } for m in matches])
    return pd.DataFrame([{
        "Season": m.season, "Round": m.round or "", "Date": m.match_date,
        "Opponent": _label(m.opponent_id), "H/A": m.where.title(),
        "Result": m.result, "Score": m.score, "Margin": m.margin,
        "Venue": m.venue, "Crowd": m.attendance,
    } for m in matches])


def past_games_page(sport, con: sqlite3.Connection) -> None:
    V = sport.vocab
    st.markdown(f"# Past {V.games.capitalize()}")
    st.caption(
        f"Every {V.game} in the database, searchable by {V.club}, opponent, "
        f"{V.season}, date and {V.venue}."
    )

    if not sport.has_past_games:
        st.info(f"Past {sport.vocab.title_case('games')} needs the "
                f"club-history tables, which are not built for "
                f"{sport.label} yet.")
        return
    if not CH.club_history_available(con):
        hint = sport.past_games_hint or "Load this sport's club-history rows."
        st.info(f"Past {sport.vocab.games} are not loaded. {hint}")
        return

    cov = CH.coverage(con)
    clubs = CH.clubs_with_history(con)
    seasons = CH.seasons_available(con)
    venues = CH.venues_available(con)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(V.games.capitalize(), f"{cov['matches']:,}")
    m2.metric(f"{V.season.capitalize()}s",
              f"{cov['season_from']}–{cov['season_to']}")
    m3.metric(V.clubs.capitalize(), len(clubs))
    m4.metric(V.venues.capitalize(), len(venues))
    if cov["excluded"]:
        st.caption("Excluded as not cleanly linked: " + ", ".join(
            f"{n:,} {status}" for status, n in cov["excluded"].items()))

    # -- filters ---------------------------------------------------------
    f1, f2, f3 = st.columns(3)
    club = f1.selectbox(V.club.capitalize(), [ANY, *clubs],
                        format_func=lambda c:
                        c if c == ANY else _label(c), key="pg_club")
    opponent_choices = [c for c in clubs if c != club]
    opponent = f2.selectbox("Opponent", [ANY, *opponent_choices],
                            format_func=lambda c: c if c == ANY else _label(c),
                            key="pg_opponent")
    venue = f3.selectbox(V.venue.capitalize(), [ANY, *venues], key="pg_venue")

    g1, g2 = st.columns([2, 1])
    lo, hi = g1.select_slider(
        "Years", options=sorted(seasons),
        value=(min(seasons), max(seasons)), key="pg_years")
    scope = g2.selectbox(
        f"{V.game.capitalize()} type", ["all", "home_and_away", "finals"],
        format_func=lambda s: {"all": f"All {V.games}",
                               "home_and_away": "Home and away",
                               "finals": V.postseason.capitalize()}[s],
        key="pg_scope")

    with st.expander("More filters"):
        d1, d2, d3 = st.columns(3)
        use_date = d1.checkbox("Filter by exact date", key="pg_use_date")
        # Bound the picker to the data. A date outside it is not a search
        # that returns nothing, it is a search that cannot mean anything.
        first = dt.date(min(seasons), 1, 1)
        last = dt.date(max(seasons), 12, 31)
        on_date = d1.date_input(
            "Date", value=dt.date(max(seasons), 1, 1),
            min_value=first, max_value=last,
            key="pg_date", disabled=not use_date)
        result = d2.selectbox(
            "Result", [ANY, "W", "D", "L"],
            format_func=lambda r: {ANY: "Any", "W": "Wins", "D": "Draws",
                                   "L": "Losses"}[r],
            key="pg_result", disabled=(club == ANY),
            help=f"Needs a {V.club} — a result is from someone's point of "
                 "view.")
        where = d2.selectbox(
            "Home or away", [ANY, "H", "A", "F"],
            format_func=lambda p: {
                ANY: "Any", "H": "Home", "A": "Away",
                "F": f"{V.postseason.capitalize()} (no home side)"}[p],
            key="pg_where", disabled=(club == ANY))
        min_margin = d3.number_input(
            "Minimum margin", min_value=0, max_value=200, value=0, step=5,
            key="pg_margin",
            help="Absolute margin, so it selects blowouts either way.")
        min_crowd = d3.number_input(
            "Minimum crowd", min_value=0, max_value=130000, value=0,
            step=5000, key="pg_crowd")
        round_text = d1.text_input(
            "Round", placeholder="R12, GF, PF…", key="pg_round")

    order = st.selectbox(
        "Sort by",
        ["date_desc", "date_asc", "margin_desc", "margin_asc", "crowd_desc",
         "crowd_asc"],
        format_func=lambda o: {
            "date_desc": "Most recent", "date_asc": "Oldest",
            "margin_desc": "Biggest margin", "margin_asc": "Smallest margin",
            "crowd_desc": "Biggest crowd", "crowd_asc": "Smallest crowd",
        }[o], key="pg_order")

    # -- search ----------------------------------------------------------
    club_id = None if club == ANY else club
    matches = CH.search_matches(
        con,
        club_id=club_id,
        opponent_id=None if opponent == ANY else opponent,
        season_from=lo, season_to=hi,
        venue=None if venue == ANY else venue,
        result=None if (result == ANY or club_id is None) else result,
        team_position=None if (where == ANY or club_id is None) else where,
        scope=scope,
        round_text=round_text.strip() or None,
        min_margin=min_margin or None,
        min_attendance=min_crowd or None,
        order=order, limit=2000,
    )

    if use_date:
        wanted = on_date.isoformat()
        matches = [m for m in matches if m.match_date == wanted]

    if not matches:
        st.info(f"No {V.games} for those filters.")
        return

    summary = f"{len(matches):,} {V.games}"
    if club_id:
        won = sum(m.result == "W" for m in matches)
        drew = sum(m.result == "D" for m in matches)
        summary += (f" · {won}-{drew}-{len(matches) - won - drew} (W-D-L) "
                    f"for {_label(club_id)}")
    crowds = [m.attendance for m in matches if m.attendance is not None]
    if crowds:
        summary += (f" · crowds {min(crowds):,}–{max(crowds):,}, "
                    f"average {sum(crowds) // len(crowds):,}")
    if len(matches) == 2000:
        summary += "  ·  showing the first 2,000"
    st.caption(summary)

    table = _match_table(matches, two_sided=club_id is None)
    # Keyed on there actually being home-less rows, not merely on there
    # being finals. AFL finals are played at neutral grounds and carry
    # team_position 'F'; MLB postseason games have a real home side, and
    # showing this note there told the reader the Home column was
    # meaningless when it was the one thing holding the series together.
    if club_id is None and any(m.team_position == "F" for m in matches):
        st.caption(
            f"{V.postseason.capitalize()} have no home side in the source, "
            f"so for rows marked Final the two columns are simply the two "
            f"{V.clubs}, in a fixed order — not a home-and-away split.")
    st.caption(f"Select a row to see that {V.game}'s full detail — the "
               f"score at every break, both box scores and the conditions.")
    components.clickable_match_table(
        table, matches, key="pg_matches", sport=sport, con=con,
        column_config={
            "Crowd": st.column_config.NumberColumn(format="%d"),
            "Margin": st.column_config.NumberColumn(
                format="%+d" if club_id else "%d"),
        })
    st.download_button("Download CSV", table.to_csv(index=False),
                       file_name="past_games.csv", mime="text/csv")

    if crowds and len(crowds) < len(matches):
        st.caption(
            f"{len(matches) - len(crowds):,} of these {V.games} have no "
            "recorded attendance. They are left blank rather than counted "
            "as a crowd of zero, and are excluded from the average above.")

    # -- head to head ----------------------------------------------------
    if club_id and opponent != ANY:
        rows = CH.head_to_head(con, club_id, opponent, scope=scope,
                               season_from=lo, season_to=hi)
        if rows:
            h = rows[0]
            st.markdown("#### Head to head")
            c1, c2, c3 = st.columns(3)
            c1.metric("Played", f"{h.played:,}")
            c2.metric(f"{_label(club_id)} record",
                      f"{h.wins}-{h.draws}-{h.losses}")
            c3.metric("Points", f"{h.points_for:,}–{h.points_against:,}")
