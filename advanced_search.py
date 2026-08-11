"""Streamlit Advanced Search page for any registered sport."""

from __future__ import annotations

import os
import sqlite3

import pandas as pd
import streamlit as st

import components
import core
import db_pool
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


#: Fallback examples, in AFL data. A sport declares its own in sports.py --
#: showing an NBA user `club:Hawthorn` teaches them a query that returns
#: nothing and reads as the search being broken.
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


def _examples(sport):
    return list(sport.search_examples) or EXAMPLES


def _apply_suggestion(state_key, query, term, name):
    """Swap the misspelling for the clicked name, before the form renders.

    Runs as the button's on_click callback, which Streamlit fires before
    the rerun -- the one moment the form's text can be set without
    fighting the widget that owns it. The rewritten query then compiles
    on the very rerun the click caused: one tap, corrected search.
    """
    st.session_state[state_key] = Q.replace_name_term(query, term, name)


def _suggestion_stars(sport, revision, matches) -> dict:
    """Obscurity stars for each suggested player, keyed by id.

    Two Gary Abletts are told apart by span and clubs already; the stars
    say at a glance which namesake is the household name and which is the
    long tail.
    """
    s = sport.schema
    marks = ",".join("?" for _ in matches)
    con = db_pool.get_con(sport.db, revision)
    return {
        pid: core.stars_text(obscurity)
        for pid, obscurity in con.execute(
            f"SELECT {s.player_id}, {s.obscurity} FROM {s.players} "
            f"WHERE {s.player_id} IN ({marks})",
            [pid for pid, _, _ in matches])
    }


def _suggest_close_names(sport, query, state_key, revision):
    """Did-you-mean for a name filter that found nobody.

    The matcher is the player picker's own: strict substring tiers first
    and a similarity scan only when none of them hit, so what comes back
    is the closest real spellings, each with the career span, games,
    clubs and obscurity stars that tell two namesakes apart. Each is a
    button that rewrites the query and searches again. Shown only when
    the *name* is what failed -- a name the database does contain means
    the other filters did the excluding, and repeating it back would be
    noise.
    """
    import ui_widgets

    for term in Q.name_terms(query):
        matches = ui_widgets.player_matches(term, sport, revision, limit=4)
        if not matches:
            continue
        typed = ui_widgets._player_search_key(term)
        if any(typed in ui_widgets._player_search_key(name)
               for _, name, _ in matches):
            continue
        stars = _suggestion_stars(sport, revision, matches)
        st.markdown(f"Nobody is spelled **{term}**. Closest names:")
        for pid, name, label in matches:
            st.button(
                f"{label}  ·  {stars.get(pid, '—')}",
                key=sport.k(f"dym_{term}_{pid}"),
                on_click=_apply_suggestion,
                args=(state_key, query, term, name))


def search_page(sport, con):
    """Render the reusable, URL-addressable player search page."""
    V = sport.vocab
    examples = _examples(sport)
    has_family = getattr(sport.C, "family_relationships_available", None)

    st.markdown("# Advanced Search")
    st.caption(
        f"Combine player, {V.club}, era, match-stat and career filters. "
        "Values are parameterised; only known fields and statistics can "
        "become SQL."
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
            placeholder=examples[0],
        )
        st.form_submit_button("Search", type="primary")

    with st.expander("Query syntax and examples"):
        for example in examples:
            st.code(example, language=None)
        # The always-true half. Everything here is compiled from the sport's
        # own schema, so it holds whichever database is loaded.
        st.write(
            "Repeat `club:` for AND. Use `club_any:` for OR. Supported stat "
            "scopes are `game.`, `season.`, `career.` and `avg.`. URL links "
            "may use `q=` or structured parameters such as `club=`, "
            "`games_min=` and `game_<stat>_min=`. All game-scoped conditions "
            "apply to the same match."
        )
        # The layer-specific half, only where the layer exists. Documenting
        # `family_relation:` for a sport with no family data would advertise
        # a filter whose only possible answer is "not loaded".
        if has_family:
            st.write(
                "Family fields are `family:`, `family_relation:`, "
                "`related_to:` and `relative_club:`. Valid family relations "
                "are `sibling`, `brother`, `parent_child`, `father_son`, "
                "`extended` and `spouse`."
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
        _suggest_close_names(sport, query, state_key, revision)
    else:
        st.caption(f"{len(frame):,} result{'s' if len(frame) != 1 else ''} shown.")
        shown = components.player_results_table(
            frame, sport, con, key=sport.k("search_results"))
        st.download_button(
            "Download results as CSV",
            data=shown.to_csv(index=False).encode("utf-8"),
            file_name=f"{sport.key}_player_search.csv",
            mime="text/csv",
        )

    with st.expander("SQL and parameters"):
        st.code(sql, language="sql")
        st.code(repr(params), language="python")