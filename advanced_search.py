"""Streamlit Advanced Search page for any registered sport."""

from __future__ import annotations

import os
import sqlite3

import pandas as pd
import streamlit as st

import core
import query_filters_family as Q


def _db_revision(db):
    stat = os.stat(db)
    return stat.st_mtime_ns, stat.st_size


@st.cache_data(show_spinner=False, max_entries=256)
def _run_query(sql, params, revision, _con):
    """Cache result frames while invalidating after a DB refresh."""
    return pd.read_sql_query(sql, _con, params=params)


def _set_query_param(key, value):
    """Avoid redundant browser-history/query-string updates."""
    if st.query_params.get(key) != value:
        st.query_params[key] = value


def _all_query_params(query_params) -> dict:
    """Return every value of every URL parameter.

    ``st.query_params.get(key)`` returns only the last value for a
    repeated key, which silently dropped filters from documented links
    like ``?club=Fitzroy&club=Richmond``. ``get_all`` preserves them.
    """
    try:
        return {key: query_params.get_all(key) for key in query_params}
    except AttributeError:              # plain-mapping fallback (tests)
        return dict(query_params)


EXAMPLES = [
    'club:Hawthorn captain:true games>=100 sort:obscurity',
    'captain_club:Carlton captain_year:1995..2001',
    'club:"St Kilda" club:Brisbane played:1995..2010',
    'game.disposals>=30 game.goals>=3 postseason:true',
    'season.goals>=50 debut:1980..1999 sort:score limit:50',
    'award:brownlow-medal drafted_by:Carlton',
    'family_relation:brother postseason:true sort:obscurity',
    'related_to:"Gary Ablett" relative_club:Geelong',
]


def search_page(sport, con):
    """Render the reusable, URL-addressable player search page."""
    st.markdown("# Advanced Search")
    st.caption(
        "Combine player, team, era, captaincy, family, match-stat, award and "
        "draft filters. Values are parameterised; only known fields and "
        "statistics can become SQL."
    )

    initial = Q.query_from_params(_all_query_params(st.query_params))
    state_key = sport.k("advanced_query")
    if state_key not in st.session_state:
        st.session_state[state_key] = initial

    with st.form(sport.k("advanced_search_form")):
        query = st.text_area(
            "Query",
            key=state_key,
            height=90,
            placeholder=EXAMPLES[0],
        )
        st.form_submit_button("Search", type="primary")

    with st.expander("Query syntax and examples"):
        for example in EXAMPLES:
            st.code(example, language=None)
        st.write(
            "Repeat `club:` for AND. Use `club_any:` for OR. Supported stat "
            "scopes are `game.`, `season.`, `career.` and `avg.`. Family "
            "fields are `family:`, `family_relation:`, `related_to:` and "
            "`relative_club:`. Valid family relations are `sibling`, "
            "`brother`, `parent_child`, `father_son`, `extended` and "
            "`spouse`. URL links may use `q=` or structured parameters such "
            "as `club=`, `captain=1`, `family_relation=brother`, "
            "`captain_from=`/`captain_to=`, `games_min=` and "
            "`game_disposals_min=`. All game-scoped conditions apply to the "
            "same match."
        )

    if not query.strip():
        st.info("Enter a query to search the complete player database.")
        return

    try:
        # Optional layers may provide connection-local placeholder tables.
        for helper_name in (
            "ensure_captain_table",
            "ensure_rising_star_table",
            "ensure_family_relationship_tables",
        ):
            ensure = getattr(sport.C, helper_name, None)
            if ensure:
                ensure(con)
        sql, params, spec = Q.compile_query(sport.schema, query, con=con)
        revision = _db_revision(sport.db)
        frame = _run_query(sql, tuple(params), revision, con)
    except (Q.QuerySyntaxError, ValueError) as exc:
        st.error(str(exc))
        return
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        # A schema surprise should be a red box, not a raw traceback.
        st.error(f"Database error while searching: {exc}")
        return

    # Keep the result bookmarkable and shareable.
    _set_query_param("sport", sport.key)
    _set_query_param("page", "search")
    _set_query_param("q", query)

    descriptions = Q.describe(spec)
    if descriptions:
        st.caption(" · ".join(descriptions))

    if frame.empty:
        st.info("No players match every filter.")
    else:
        if "ObscurityRaw" in frame.columns:
            frame["Rating"] = frame["ObscurityRaw"].map(core.stars_text)
            frame = frame.drop(columns=["ObscurityRaw"])
        if "Teams" in frame.columns:
            frame["Teams"] = frame["Teams"].fillna("").str.replace(
                "|", ", ", regex=False
            )
        st.caption(f"{len(frame):,} result{'s' if len(frame) != 1 else ''} shown.")
        st.dataframe(frame, hide_index=True, width="stretch")
        st.download_button(
            "Download results as CSV",
            data=frame.to_csv(index=False).encode("utf-8"),
            file_name=f"{sport.key}_player_search.csv",
            mime="text/csv",
        )

    with st.expander("SQL and parameters"):
        st.code(sql, language="sql")
        st.code(repr(params), language="python")
