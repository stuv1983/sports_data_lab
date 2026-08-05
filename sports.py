"""
sports.py -- The registry that lets one app serve multiple sports.

Each sport describes itself in its own package:

    afl/sport.py   nba/sport.py   mlb/sport.py   nfl/sport.py

each exporting `SPORT`, plus the `SCHEMA`, `STATS` and `CLUBS` that go into
it. registry.py holds the types those descriptions are built from. This file
only collects them and answers "which sport, and is it loadable" -- adding a
sport is a new package module and one line in SPORTS.

Nothing in app.py or explore.py should ever branch on `sport.key`; if it
needs to, the difference belongs in the sport's own module as a vocabulary,
schema or capability field.
"""

from afl import sport as afl_sport
from mlb import sport as mlb_sport
from nba import sport as nba_sport
from nfl import sport as nfl_sport
from registry import Sport, Vocab                      # noqa: F401  (re-export)

AFL = afl_sport.SPORT
NBA = nba_sport.SPORT
MLB = mlb_sport.SPORT
NFL = nfl_sport.SPORT

#: Schemas, stat lists and club lists under the names the rest of the
#: repository already knows them by. Each sport's constraints module reads
#: its schema from here rather than from its own package, so that a module
#: importing `sports` gets the registry fully assembled -- which is what
#: keeps the sport packages free of imports of each other.
AFL_SCHEMA, AFL_STATS = afl_sport.SCHEMA, afl_sport.STATS
AFL_CLUBS, AFL_CLUB_LINEAGE = afl_sport.CLUBS, afl_sport.CLUB_LINEAGE
AFL_VENUE_ALIASES = afl_sport.VENUE_ALIASES
NBA_SCHEMA, NBA_STATS = nba_sport.SCHEMA, nba_sport.STATS
MLB_SCHEMA, MLB_STATS = mlb_sport.SCHEMA, mlb_sport.STATS
NFL_SCHEMA, NFL_STATS = nfl_sport.SCHEMA, nfl_sport.STATS
NFL_CLUBS = nfl_sport.CLUBS


# -------------------------------------------------------------- registry

SPORTS = {s.key: s for s in (AFL, NBA, MLB, NFL)}
DEFAULT = AFL.key


def get(key):
    return SPORTS.get(key, SPORTS[DEFAULT])


def selectable():
    """Sports worth loading: enabled, with a database actually present."""
    out = [s for s in SPORTS.values() if s.enabled and s.exists()]
    return out or [SPORTS[DEFAULT]]


def offerable():
    """Sports the picker shows: the loadable ones plus the announced ones."""
    ready = selectable()
    keys = {s.key for s in ready}
    out = ready + [s for s in SPORTS.values()
                   if s.enabled and s.preview and s.key not in keys]
    return sorted(out, key=lambda s: list(SPORTS).index(s.key))


def picker(st, key="sport"):
    """
    Render the sport switcher and return the chosen Sport.

    Call this before anything else touches the database. When the sport
    changes, every widget key belonging to the previous sport is dropped,
    which is what stops a stale AFL axis from leaking into an NBA board.

    A sport without a database is still offered, marked "coming soon". The
    caller is responsible for checking `exists()` before touching `.C` or
    the database -- a preview sport may have neither.
    """
    options = offerable()
    if len(options) == 1:
        st.session_state[key] = options[0].key
        return options[0]

    labels = {s.key: f"{s.icon}  {s.label}" + ("" if s.exists()
                                               else "  (coming soon)")
              for s in options}
    chosen = st.sidebar.radio(
        "Sport", [s.key for s in options],
        format_func=lambda k: labels[k], key=key, horizontal=True)

    previous = st.session_state.get("_sport_prev")
    if previous and previous != chosen:
        stale = [k for k in st.session_state
                 if isinstance(k, str) and k.startswith(f"{previous}:")]
        for k in stale:
            del st.session_state[k]
        st.session_state.pop("loaded", None)
        st.session_state.pop("cell", None)
    st.session_state["_sport_prev"] = chosen
    return get(chosen)
