import re
import streamlit as st

import db_pool
import sports
import labels
import venue_states


# -------------------------------------------------------------- lookups
@st.cache_data(show_spinner=False)
def venue_options(sport_key, db, revision):
    """Real ground names, most-used first, with the alias the grid uses."""
    s = sports.get(sport_key).schema
    rows = db_pool.get_con(db, revision).execute(
        f"SELECT {s.venue}, COUNT(*) c, MIN({s.season}), MAX({s.season}) "
        f"FROM {s.games} GROUP BY {s.venue} ORDER BY c DESC").fetchall()
    alias = sports.get(sport_key).venue_display
    out = []
    for v, c, lo, hi in rows:
        extra = f"  ({alias[v]})" if v in alias else ""
        out.append((v, f"{v}{extra}  ·  {lo}-{hi}  ·  {c:,} {sports.get(sport_key).vocab.games}"))
    return out


@st.cache_data(show_spinner=False)
def marquee_event_options(sport_key, db, revision):
    """Marquee fixture names actually tagged in this database."""
    module = sports.get(sport_key).C
    lookup = getattr(module, "marquee_events", None)
    if lookup is None:
        return []
    try:
        return list(lookup(db_pool.get_con(db, revision)))
    except Exception:
        return []


PLAYER_MATCH_LIMIT = 30


def _player_search_key(value):
    """Search-friendly name key: punctuation and spacing are interchangeable."""
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


@st.cache_data(show_spinner=False)
def player_options(sport_key, db, revision):
    """Every player, with an unambiguous label and a search-only name key."""
    s = sports.get(sport_key).schema
    rows = db_pool.get_con(db, revision).execute(
        f"SELECT {s.player_id}, {s.player}, {s.debut_season}, "
        f"{s.final_season}, {s.career_games}, {s.clubs_hist} "
        f"FROM {s.players} ORDER BY {s.player}").fetchall()
    return [(pid, nm, _player_label(nm, d, f, g, cl),
             _player_search_key(nm))
            for pid, nm, d, f, g, cl in rows]


def _player_label(name, debut, final, games, clubs):
    parts = []
    if debut and final:
        parts.append(f"{debut}-{final}")
    elif debut or final:
        parts.append(str(debut or final))
    if games:
        parts.append(f"{games}g")
    if clubs:
        parts.append(str(clubs).replace("|", "/"))
    return f"{name} ({', '.join(parts)})" if parts else str(name)


def player_matches(query, sport, db_revision, limit=PLAYER_MATCH_LIMIT):
    """Return a small, relevance-ranked list instead of every player."""
    q = _player_search_key(query)
    if not q:
        return []

    q_words = q.split()
    ranked = []
    for pid, name, label, search_key in player_options(
            sport.key, sport.db, db_revision):
        words = search_key.split()
        if search_key == q:
            rank = 0
        elif search_key.startswith(q):
            rank = 1
        elif any(word.startswith(q) for word in words):
            rank = 2
        elif all(any(token in word for word in words) for token in q_words):
            rank = 3
        elif q in search_key:
            rank = 4
        else:
            continue
        ranked.append((rank, len(name), name.casefold(), pid, name, label))

    ranked.sort()
    return [(pid, name, label)
            for _, _, _, pid, name, label in ranked[:limit]]


def player_picker(key, sport, db_revision, label="Player name", default_name=""):
    """Text-first player picker with at most 30 matching choices."""
    query_key = f"{key}_query"
    choice_key = f"{key}_choice"
    if query_key not in st.session_state:
        st.session_state[query_key] = default_name

    query = st.text_input(
        label, key=query_key, placeholder="Start typing a player name…")
    if not query.strip():
        st.caption("Type part of a first name or surname.")
        return None

    matches = player_matches(query, sport, db_revision)
    if not matches:
        st.warning(f"No player matches ‘{query.strip()}’.")
        return None

    options = [pid for pid, _, _ in matches]
    details = {pid: (name, player_label)
               for pid, name, player_label in matches}

    if (choice_key in st.session_state
            and st.session_state[choice_key] not in options):
        del st.session_state[choice_key]

    pid = st.selectbox(
        "Matching players", options, index=0, key=choice_key,
        format_func=lambda player_id: details[player_id][1],
        label_visibility="collapsed")

    if len(matches) == PLAYER_MATCH_LIMIT:
        st.caption(f"Showing the first {PLAYER_MATCH_LIMIT} matches — "
                   "type more letters to narrow the list.")
    return pid, details[pid][0]


# ------------------------------------------------------- axis definition
def _stat_label(stat):
    return labels.words(stat)

_STAT_SCOPE_LABELS = {
    "X+ of a stat in one game":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\nin a {V.game}",
    "X+ of a stat in one season":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\nin a {V.season}",
    "X+ of a stat in a career":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\ncareer",
    "Season average of a stat":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])} avg\nin a {V.season}",
    "Career average of a stat":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])} avg\nper {V.game}",
    "X+ of a stat in a final":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\nin a {V.postseason_one}",
    "Finals average of a stat":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])} avg\nin {V.postseason}",
}


def _builder_label(kind, vocab):
    """Render shared builder keys in the selected sport's vocabulary."""
    replacements = (
        (r"\bgoals\b", vocab.score),
        (r"\bclubs\b", vocab.clubs),
        (r"\bclub\b", vocab.club),
        (r"\bfinals\b", vocab.postseason),
        (r"\bfinal\b", vocab.postseason_one),
    )
    label = kind
    for pattern, word in replacements:
        label = re.sub(pattern, word, label)
    return label


def axis_widget(key, default_type, defaults, sport, db_revision, available_builders):
    """One axis of the grid. Returns (label, constraint) or (label, None)."""
    defaults = defaults or {}
    kinds = available_builders
    default_index = kinds.index(default_type) if default_type in kinds else 0
    kind = st.selectbox("Type", kinds, index=default_index,
                        key=f"{key}_kind", label_visibility="collapsed",
                        format_func=lambda k: _builder_label(k, sport.vocab))
    
    C = sport.C
    SCHEMA = sport.schema
    V = sport.vocab

    fn, argnames = C.BUILDERS[kind]
    args = []

    for a in argnames:
        wk = f"{key}_{a}"
        if a == "club":
            clubs = list(SCHEMA.clubs)
            want = defaults.get("club", clubs[0])
            idx = clubs.index(want) if want in clubs else 0
            args.append(st.selectbox(V.club.capitalize(), clubs,
                                     key=wk, index=idx))
        elif a == "venue":
            vs = venue_options(sport.key, sport.db, db_revision)
            names = [v for v, _ in vs]
            want = defaults.get("venue", names[0] if names else "")
            idx = names.index(want) if want in names else 0
            pick = st.selectbox(V.venue.capitalize(), range(len(vs)),
                                index=idx,
                                format_func=lambda i, options=vs: options[i][1],
                                key=wk)
            args.append(vs[pick][0] if vs else None)
        elif a == "state":
            states = list(venue_states.STATE_NAMES)
            want = defaults.get("state", states[0])
            idx = states.index(want) if want in states else 0
            args.append(st.selectbox(
                "State", states, index=idx, key=wk,
                help="50 states plus Washington, D.C. and Puerto Rico. "
                     "A jurisdiction with no recorded game returns no players."))
        elif a == "player_id":
            selected = player_picker(
                wk, sport, db_revision, default_name=str(defaults.get("player", "")))
            if selected is None:
                args.append(None)
                st.session_state[f"{wk}_label"] = "—"
            else:
                pid, player_name = selected
                args.append(pid)
                st.session_state[f"{wk}_label"] = player_name
        elif a == "kind":
            draft_kinds = list(getattr(C, "DRAFT_TYPES", ()))
            args.append(st.selectbox("Draft type", draft_kinds, key=wk))
        elif a == "source":
            args.append(st.text_input("Recruited from",
                                      defaults.get("source", ""), key=wk))
        elif a == "award":
            names = list(C.AWARD_SLUGS)
            want = defaults.get("award")
            idx = names.index(want) if want in names else 0
            pick = st.selectbox("Award", names, index=idx, key=wk)
            args.append(C.AWARD_SLUGS[pick])
        elif a == "times":
            label = ("How many such games" if kind == "X+ games with Y+ of a stat"
                     else "How many" if any(
                         token in kind for token in
                         ("Grand Finals", "Super Bowls", "World Series",
                          "NBA Finals", "championships"))
                     else "Times")
            args.append(st.number_input(label, min_value=1,
                                        value=int(defaults.get("times", 1)),
                                        step=1, key=wk))
        elif a == "place":
            args.append(st.number_input(
                "Finishing position", min_value=1,
                value=int(defaults.get("place", 5)), step=1, key=wk))
        elif a == "votes":
            args.append(st.number_input(
                "Brownlow votes", min_value=1,
                value=int(defaults.get("votes", 20)), step=1, key=wk))
        elif a == "avg":
            caption = {
                "Season average of a stat":
                    f"per-{V.game} average across a {V.season}",
                "Career average of a stat":
                    f"per-{V.game} average across a career",
                "Finals average of a stat":
                    f"per-{V.game} average across {V.postseason}",
            }.get(kind, f"{V.score} per {V.postseason_one}")
            args.append(st.number_input(caption, value=1.0, step=0.5,
                                        key=wk))
        elif a == "min_games":
            args.append(st.number_input(
                f"Minimum {V.games}",
                min_value=1, step=1,
                value=int(defaults.get("min_games", C.CAREER_AVG_MIN_GAMES)),
                key=wk,
                help=f"An average over a handful of {V.games} is not a "
                     f"career average in any sense the question means."))
        elif a == "player":
            args.append(st.text_input("Player", defaults.get("player", ""),
                                      key=wk))
        elif a == "award_axis":
            choices = list(getattr(C, "AWARD_AXIS_CHOICES", ()))
            keys = [k for k, _ in choices]
            want = defaults.get("award_axis")
            idx = keys.index(want) if want in keys else 0
            pick = st.selectbox("Award", range(len(choices)), index=idx,
                                format_func=lambda i, options=choices:
                                    options[i][0],
                                key=wk)
            args.append(choices[pick][0] if choices else None)
        elif a == "position":
            positions = list(getattr(C, "POSITIONS", ()))
            want = defaults.get("position")
            idx = positions.index(want) if want in positions else 0
            args.append(st.selectbox("Position", positions, index=idx, key=wk)
                        if positions else None)
        elif a == "average":
            args.append(st.number_input(
                "Batting average", min_value=0.0, max_value=1.0,
                value=float(defaults.get("average", 0.300)),
                step=0.005, format="%.3f", key=wk))
        elif a == "war":
            fallback = 5.0 if kind == "X+ WAR in a season" else 50.0
            args.append(st.number_input(
                "Minimum WAR", value=float(defaults.get("war", fallback)),
                step=0.5, format="%.1f", key=wk))
        elif a == "min_plate_appearances":
            args.append(st.number_input(
                "Minimum plate appearances", min_value=1, step=1,
                value=int(defaults.get("min_plate_appearances", 502)), key=wk,
                help="A rate needs a floor: without one a September "
                     "call-up going 1-for-2 bats .500 for the season. "
                     "Estimated as at-bats plus walks — an at-bats floor "
                     "would exclude Ted Williams's .406 in 1941."))
        elif a == "derby":
            choices = list(getattr(C, "DERBY_CHOICES", ()))
            keys = [k for k, _ in choices]
            want = defaults.get("derby")
            idx = keys.index(want) if want in keys else 0
            pick = st.selectbox("Derby", range(len(choices)), index=idx,
                                format_func=lambda i, options=choices:
                                    options[i][1],
                                key=wk)
            args.append(choices[pick][0] if choices else None)
        elif a == "event":
            events = marquee_event_options(sport.key, sport.db, db_revision)
            want = defaults.get("event")
            idx = events.index(want) if want in events else 0
            args.append(st.selectbox("Match", events, index=idx, key=wk)
                        if events else None)
        elif a == "rivalry":
            choices = list(getattr(C, "RIVALRY_CHOICES", ()))
            keys = [k for k, _ in choices]
            want = defaults.get("rivalry")
            idx = keys.index(want) if want in keys else 0
            pick = st.selectbox("Rivalry", range(len(choices)), index=idx,
                                format_func=lambda i, options=choices:
                                    options[i][1],
                                key=wk)
            args.append(choices[pick][0] if choices else None)
        elif a in ("ground_status", "ground_metric"):
            choices = list(getattr(
                C, "GROUND_STATUS_CHOICES" if a == "ground_status"
                else "GROUND_METRIC_CHOICES", ()))
            values = [value for value, _ in choices]
            want = defaults.get(a)
            idx = values.index(want) if want in values else 0
            pick = st.selectbox(
                "Match status" if a == "ground_status" else "Measure",
                range(len(choices)), index=idx,
                format_func=lambda i, options=choices: options[i][1], key=wk)
            chosen = choices[pick][0] if choices else None
            if a == "ground_metric" and chosen not in (None, "games", "wins"):
                warning = sport.stat_era_warning(
                    "goals" if chosen == "score" else chosen)
                if warning:
                    st.caption(f"⚠ {warning}")
            args.append(chosen)
        elif a in ("stat", "stat_a", "stat_b"):
            stats = list(SCHEMA.stats)
            want = defaults.get(a, stats[0])
            idx = stats.index(want) if want in stats else 0
            chosen = st.selectbox(labels.words(a), stats, key=wk,
                                  index=idx, format_func=labels.title)
            warning = sport.stat_era_warning(chosen)
            if warning:
                st.caption(f"⚠ {warning}")
            args.append(chosen)
        elif a in ("goals", "clubs", "games"):
            word = {"goals": V.score, "clubs": V.clubs,
                    "games": V.games}[a]
            fallback = {"goals": 30, "clubs": 2, "games": 30}[a]
            args.append(st.number_input(
                word.capitalize(), min_value=1,
                value=int(defaults.get(a, fallback)), step=1, key=wk))
        else:
            year_kinds = {
                "Played between seasons", "Debuted between seasons",
                "Drafted between years", "All-Australian between years",
                "Rising Star nominee in season",
                "Rising Star nominee between seasons",
                "Rising Star nominee for club between seasons",
            }
            if kind in year_kinds and a in ("from", "to", "season"):
                lo, hi = season_span(sport.key, sport.db, db_revision)
                fallback = max(lo, hi - 46) if a == "from" else hi
                args.append(st.number_input(
                    a.replace("_", " "), min_value=lo, max_value=hi,
                    value=int(defaults.get(a, fallback)), step=1, key=wk))
            else:
                fallback = {
                    "from": 1, "to": 10, "clubs": 2,
                    "x": 5, "y": 5, "x_a": 30, "x_b": 3,
                }.get(a, 30)
                args.append(st.number_input(
                    a.replace("_", " ").upper(),
                    value=int(defaults.get(a, fallback)),
                    step=1, key=wk,
                    help="This is the X in the builder name above."))

    label = kind
    if kind == "Played for club":
        label = args[0]
    elif kind == "Teammate of…":
        label = f"{st.session_state.get(f'{key}_player_id_label', '—')} teammate"
    elif kind == "Played with…":
        label = f"played with\n{st.session_state.get(f'{key}_player_id_label', '—')}"
    elif kind in ("Played at venue", "Won a final at venue"):
        verb = ("played at" if kind.startswith("Played")
                else f"won a {V.postseason_one} at")
        label = f"{verb}\n{args[0]}"
    elif kind == "Ground performance":
        statuses = dict(getattr(C, "GROUND_STATUS_CHOICES", ()))
        metrics = dict(getattr(C, "GROUND_METRIC_CHOICES", ()))
        label = (f"{args[3]}+ {metrics.get(args[2], args[2])}\n"
                 f"{statuses.get(args[1], args[1]).lower()} at {args[0]}")
    elif kind == "Played in state":
        label = f"played in\n{args[0]}"
    elif kind == "X+ finals games":
        label = f"{args[0]}+ {V.postseason} {V.games}"
    elif kind == "Goal average in finals":
        label = f"{args[0]}+ {V.score} avg\nin {V.postseason}"
    elif kind == "First career game for club":
        label = f"{args[0]}\nfirst career {V.game}"
    elif kind in ("Winning record in a derby", "Losing record in a derby"):
        outcome = "winning" if kind.startswith("Winning") else "losing"
        label = f"{C.DERBY_LABELS.get(args[0], args[0])}\n{outcome} record"
    elif kind == "X+ games in a derby":
        label = f"{args[1]}+ {C.DERBY_LABELS.get(args[0], args[0])}\n{V.games}"
    elif kind == "Played in a derby":
        label = f"played a\n{C.DERBY_LABELS.get(args[0], args[0])}"
    elif kind == "Winning record in a marquee match":
        label = f"{args[0]}\nwinning record"
    elif kind == "X+ marquee matches":
        label = f"{args[1]}+ {args[0]}\n{V.games}"
    elif kind == "Played in a marquee match":
        label = f"played\n{args[0]}"
    elif kind == "Winning record in a rivalry":
        label = f"{C.RIVALRY_LABELS.get(args[0], args[0])}\nwinning record"
    elif kind == "X+ games in a rivalry":
        label = f"{args[1]}+ {V.games} in\n{C.RIVALRY_LABELS.get(args[0], args[0])}"
    elif kind == "150+ / X+ career games":
        label = f"{args[0]}+ {V.games} played"
    elif kind == "X+ goals at 2+ clubs":
        label = f"{args[0]}+ {V.score}\nat {args[1]}+ {V.clubs}"
    elif kind == "X+ games at 2+ clubs":
        label = f"{args[0]}+ {V.games}\nat {args[1]}+ {V.clubs}"
    elif (kind.startswith(("Played in X+", "Won X+", "Lost X+"))
          and args):
        label = kind.replace("X+", f"{args[0]}+")
    elif kind == "Fewer than X career games":
        label = f"under {args[0]} {V.games}"
    elif kind == "X+ WAR in a season":
        label = f"{args[0]:g}+ WAR\nin a {V.season}"
    elif kind == "X+ career WAR":
        label = f"{args[0]:g}+ career WAR"
    elif kind == "Two stats in the same game":
        label = f"{args[1]}+ {args[0]} &\n{args[3]}+ {args[2]}"
    elif kind == "X+ games with Y+ of a stat":
        label = f"{args[2]}+ {V.games} with\n{args[1]}+ {_stat_label(args[0])}"
    elif kind in _STAT_SCOPE_LABELS:
        label = _STAT_SCOPE_LABELS[kind](args, V)
    elif kind == "Top X Brownlow finish":
        label = f"top-{args[0]} Brownlow finish"
    elif kind == "Exact Brownlow finish":
        label = f"finished {args[0]} in Brownlow"
    elif kind == "Top X Brownlow finish X+ times":
        label = f"top-{args[0]} Brownlow\n{args[1]}+ times"
    elif kind == "X+ Brownlow votes in a season":
        label = f"{args[0]}+ Brownlow votes\nin a season"

    if kind in ("Teammate of…", "Played with…") and (
            not args or args[0] is None):
        return label, None

    try:
        return label, fn(*args)
    except Exception as e:
        st.warning(str(e))
        return label, None


@st.cache_data(show_spinner=False)
def season_span(sport_key, db, revision):
    s = sports.get(sport_key).schema
    return db_pool.get_con(db, revision).execute(
        f"SELECT MIN({s.season}), MAX({s.season}) FROM {s.games}").fetchone()
