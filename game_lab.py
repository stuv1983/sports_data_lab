"""
game_lab.py -- Database-driven AFL games.

Guess the Player. Each clue is a real SQL predicate, so the game can show
how many players still fit and clues can be ordered by how much they
actually narrow the field. Wrong guesses return directional feedback
rather than just "no".

Two clue styles:

  Database profile   Banded facts straight off the `players` row -- era,
                     longevity, scoring, clubs. Always available.

  Gridley criteria   Clues drawn from criteria that have appeared on real
                     Gridley boards (historic_grids.py), phrased the way
                     Gridley phrases them. Only criteria this database
                     genuinely supports are in the bank: a criterion the
                     parser declines is never reworded into a different
                     condition to fill a slot.

Reached through explore.game_lab_page(), which app.py calls. This module
reads AFL-only columns and AFL-only criterion wording, so explore.py only
delegates here for the AFL sport.
"""

import streamlit as st

import constraints as C
import historic_grids as HG
import parse_criteria as P

# Target pools. Obscurity is the fame proxy from build_db.py: low = famous.
DIFFICULTY = {
    "Easy": "career_games >= 150 AND obscurity <= 30",
    "Medium": "career_games >= 100 AND obscurity <= 55",
    "Hard": "career_games >= 50",
}

PLAYER_COLS = ("player_id, player, debut_season, final_season, career_games, "
               "career_goals, career_brownlow, finals_played, clubs_hist, "
               "n_clubs, best_disposals, best_goals, obscurity")


def _decade(year):
    return int(year) // 10 * 10


def _band(n, edges):
    """Bucket a value into (lo, hi) using ascending upper edges."""
    lo = 0
    for e in edges:
        if n <= e:
            return lo, e
        lo = e + 1
    return lo, 10 ** 6


def _span(lo, hi):
    return f"{lo}+" if hi >= 10 ** 6 else f"{lo}–{hi}"


def clue_ladder(t):
    """
    (label, text, sql, params) ordered broad to narrow.

    Each clue's sql is the predicate that clue asserts, so the remaining
    count after N clues is exactly the count of players satisfying the
    first N predicates. The early clues are banded rather than exact --
    an exact finals or goals tally is close to unique on its own and
    collapses the game in one step.
    """
    (_pid, name, debut, final, games, goals, brownlow, finals,
     clubs_hist, n_clubs, best_disp, best_goals, _obsc) = t

    goals, brownlow = int(goals or 0), int(brownlow or 0)
    d = _decade(debut)
    g_lo, g_hi = _band(games, [99, 149, 199, 249, 299])
    f_lo, f_hi = _band(finals, [0, 5, 15])
    k_lo, k_hi = _band(goals, [9, 49, 199, 499])
    b_lo, b_hi = _band(brownlow, [0, 9, 29])
    clubs = [c for c in clubs_hist.split("|") if c]
    initials = " ".join(w[0] + "." for w in name.split() if w)

    ladder = [
        ("Era",
         f"Debuted in the {d}s and played {n_clubs} "
         f"club{'s' if n_clubs != 1 else ''}.",
         "debut_season BETWEEN ? AND ? AND n_clubs = ?",
         [d, d + 9, n_clubs]),

        ("Longevity",
         f"Played {_span(g_lo, g_hi)} games, {_span(f_lo, f_hi)} of them finals.",
         "career_games BETWEEN ? AND ? AND finals_played BETWEEN ? AND ?",
         [g_lo, g_hi, f_lo, f_hi]),

        ("Scoring",
         f"Kicked {_span(k_lo, k_hi)} career goals and polled "
         f"{_span(b_lo, b_hi)} Brownlow votes.",
         "career_goals BETWEEN ? AND ? AND career_brownlow BETWEEN ? AND ?",
         [k_lo, k_hi, b_lo, b_hi]),

        ("Clubs",
         f"Played for {', '.join(clubs)}.",
         "clubs_hist = ?",
         [clubs_hist]),

        ("Career span",
         f"First game {debut}, last game {final}.",
         "debut_season = ? AND final_season = ?",
         [debut, final]),

        ("Initials",
         f"Initials {initials}.",
         "player LIKE ?",
         [name.split()[0][0] + "%"]),
    ]

    if best_disp is not None or best_goals is not None:
        parts, sql, params = [], [], []
        if best_disp is not None:
            disp = int(best_disp)
            parts.append(f"{max(disp - 2, 0)}–{disp + 2} disposals")
            sql.append("best_disposals BETWEEN ? AND ?")
            params += [disp - 2, disp + 2]
        if best_goals is not None:
            bg = int(best_goals)
            parts.append(f"{bg} goals")
            sql.append("best_goals = ?")
            params.append(bg)
        ladder.insert(3, ("Best game",
                          "Career-best single game: " + " and ".join(parts) + ".",
                          " AND ".join(sql), params))
    return ladder


# ------------------------------------------------ Gridley criterion bank
# Criteria that have actually appeared on a Gridley board, reused as clues.
# The point of drawing from history rather than inventing clue text is that
# the questions then sound like the puzzle: "kicked 100+ career goals",
# not "career_goals >= 100".

#: Two clues from the same family restate one fact. "PLAYED IN A FINAL"
#: and "5+ FINALS GAMES" are not two pieces of information about a player;
#: the second makes the first redundant. One clue per family, at most.
FAMILIES = (
    ("teammate", ("teammate",)),
    ("club", ("first career game", "one-club", "multi-club", "clubs")),
    ("era", ("played in", "1990s", "2000s", "2010s", "2020s")),
    ("finals", ("final", "grand final", "premiership", "flag")),
    ("career games", ("games played", "games two diff", "fewer games")),
    ("career goals", ("goals career", "career goals", "goals two diff")),
    ("single game", ("in a game", "game", "&")),
    ("season average", ("avg", "average")),
    ("club honours", ("goalkicker", "wooden spoon")),
    ("major awards", ("all australian", "brownlow", "coleman", "norm smith")),
)


def _family(text):
    t = " ".join(text.lower().split())
    # A bare club name carries no family keyword at all, and two of them
    # ("Geelong", "Collingwood") is exactly the redundancy the family rule
    # exists to prevent — so match the vocabulary lists before the words.
    if t in {c.lower() for c in C.CLUBS}:
        return "club"
    if any(alias in t for alias in C.VENUE_ALIASES):
        return "venue"
    for name, words in FAMILIES:
        if any(w in t for w in words):
            return name
    return "other"


#: A clue that leaves nearly everyone in the pool has told the player
#: nothing; one that leaves nearly nobody ends the game instead of
#: narrowing it. Both are rejected before the ladder is built.
TOO_BROAD = 0.90        # share of the pool still standing
TOO_NARROW = 2          # players still standing


def _observed_criteria():
    """Every distinct criterion text seen on a captured Gridley board."""
    seen = []
    for grid in HG.GRIDS:
        for text in grid.criteria:
            if text not in seen:
                seen.append(text)
    return seen


@st.cache_data(show_spinner=False)
def criterion_bank(_con, sport_key):
    """
    (text, label, sql, params, eligible) for every supported criterion.

    Cached per sport. Criteria the parser declines -- family links and any optional dataset
    not loaded locally -- are simply absent: an unanswerable
    criterion is not quietly turned into an answerable one to pad the bank.
    """
    bank = []
    for text in _observed_criteria():
        cn, label = P.parse(text)
        if cn is None:
            continue
        try:
            n = C.count(_con, [cn])
        except Exception:
            continue                    # optional layer not imported
        if n:
            bank.append((text, label, cn[0], tuple(cn[1]), n))
    return bank


def _fits(con, sql, params, pid):
    return bool(con.execute(
        f"SELECT 1 FROM players p WHERE p.player_id = ? AND p.player_id "
        f"IN ({sql})", (pid, *params)).fetchone())


def _remaining_with(con, pool, chosen):
    """Pool size after every chosen criterion is applied."""
    where = [pool] + [f"p.player_id IN ({c[2]})" for c in chosen]
    params = [v for c in chosen for v in c[3]]
    return con.execute(
        f"SELECT COUNT(*) FROM players p WHERE {' AND '.join(where)}",
        params).fetchone()[0]


def gridley_ladder(con, pool, pool_size, target_pid, sport_key, max_clues=5):
    """
    A clue ladder built from real Gridley criteria, broadest first.

    Selection rules, in order:
      1. keep only criteria the target actually satisfies;
      2. drop anything that leaves almost everyone or almost nobody;
      3. one clue per family, so two clues never restate one fact;
      4. order broad to narrow, so the game tightens instead of ending.

    Returns the same (label, text, sql, params) shape as clue_ladder(),
    except that sql is a subquery on player_id rather than a predicate on
    the players row, so it needs _remaining_with() rather than remaining().
    """
    fitting = []
    for text, label, sql, params, n in criterion_bank(con, sport_key):
        if n > pool_size * TOO_BROAD or n < TOO_NARROW:
            continue
        if not _fits(con, sql, params, target_pid):
            continue
        fitting.append((text, label, sql, params, n))

    fitting.sort(key=lambda x: -x[4])           # broad first

    ladder, used = [], set()
    for text, label, sql, params, n in fitting:
        fam = _family(text)
        if fam in used:
            continue
        candidate = (label.replace("\n", " "), text, sql, params)
        after = _remaining_with(con, pool, [c for c in ladder] + [candidate])
        if after < TOO_NARROW and ladder:
            continue                            # would end the game outright
        used.add(fam)
        ladder.append(candidate)
        if len(ladder) >= max_clues:
            break
    return ladder


def remaining(con, pool, ladder, revealed):
    """How many players in the pool still fit the clues shown so far."""
    sql = [pool] + [c[2] for c in ladder[:revealed]]
    params = [p for c in ladder[:revealed] for p in c[3]]
    return con.execute(
        f"SELECT COUNT(*) FROM players WHERE {' AND '.join(sql)}",
        params).fetchone()[0]


def feedback(target, guess):
    """Directional hints comparing a wrong guess to the target."""
    arrow = lambda a, b: "same" if a == b else ("higher ↑" if a > b else "lower ↓")
    t_clubs = set(target[8].split("|"))
    g_clubs = set(guess[8].split("|"))
    shared = t_clubs & g_clubs
    return {
        "Debut season": f"{guess[2]} — target is {arrow(target[2], guess[2])}",
        "Career games": f"{guess[4]} — target is {arrow(target[4], guess[4])}",
        "Finals": f"{guess[7]} — target is {arrow(target[7], guess[7])}",
        "Career goals": f"{int(guess[5] or 0)} — target is "
                        f"{arrow(int(target[5] or 0), int(guess[5] or 0))}",
        "Clubs": (f"shares {', '.join(sorted(shared))}" if shared
                  else "no club in common"),
    }


def _pick_target(con, pool):
    row = con.execute(
        f"SELECT {PLAYER_COLS} FROM players WHERE {pool} "
        "ORDER BY RANDOM() LIMIT 1").fetchone()
    return row


def _reset(con, pool):
    st.session_state.gl_target = _pick_target(con, pool)
    st.session_state.gl_revealed = 1
    st.session_state.gl_guesses = []
    st.session_state.gl_solved = False
    for key in ("gl_guess_query", "gl_guess_choice"):
        st.session_state.pop(key, None)


CLUE_STYLES = ("Database profile", "Gridley criteria")


def game_lab_page(sport, con, player_picker):
    st.markdown("# Game Lab")
    st.caption("Turning the database into playable challenges.")
    st.markdown("### Guess the Player")

    difficulty = st.radio("Difficulty", list(DIFFICULTY), horizontal=True,
                          key="gl_difficulty")
    pool = DIFFICULTY[difficulty]

    style = st.radio("Clue style", CLUE_STYLES, horizontal=True,
                     key="gl_style")
    if style == "Gridley criteria":
        st.caption("Clues are criteria that have appeared on real Gridley "
                   "boards. Criteria this database cannot answer — for "
                   "example family links — are left out rather than reworded. "
                   "Optional captain and Rising Star clues appear after their "
                   "local datasets have been loaded.")

    # The two styles produce different ladders, so a style switch has to
    # restart the reveal count as well as a difficulty switch. Without
    # this, four clues revealed on a six-clue profile ladder carries over
    # onto a three-clue Gridley ladder and shows the game already over.
    shape = (pool, style)
    if ("gl_target" not in st.session_state
            or st.session_state.get("gl_shape") != shape):
        restart = st.session_state.get("gl_pool") != pool
        st.session_state.gl_shape = shape
        st.session_state.gl_pool = pool
        if restart or "gl_target" not in st.session_state:
            _reset(con, pool)
        else:
            st.session_state.gl_revealed = 1

    if st.button("New mystery player", key="gl_new"):
        _reset(con, pool)

    target = st.session_state.gl_target
    if not target:
        st.error("No eligible player found for this difficulty.")
        return

    pool_size = con.execute(
        f"SELECT COUNT(*) FROM players WHERE {pool}").fetchone()[0]
    st.caption(f"{pool_size:,} players in the {difficulty.lower()} pool.")

    if style == "Gridley criteria":
        ladder = gridley_ladder(con, pool, pool_size, target[0], sport.key)
        count_after = lambda n: _remaining_with(con, pool, ladder[:n])
        if not ladder:
            st.warning("No Gridley criterion in the bank both fits this "
                       "player and narrows the pool usefully. Try another "
                       "mystery player, or the database profile clues.")
            return
    else:
        ladder = clue_ladder(target)
        count_after = lambda n: remaining(con, pool, ladder, n)

    revealed = min(st.session_state.gl_revealed, len(ladder))

    for i, (label, text, _sql, _params) in enumerate(ladder[:revealed], start=1):
        n = count_after(i)
        st.write(f"**Clue {i} · {label}:** {text}")
        st.caption(f"{n:,} player{'s' if n != 1 else ''} still fit.")

    if revealed < len(ladder) and not st.session_state.gl_solved:
        nxt = count_after(revealed + 1)
        if st.button(f"Reveal clue {revealed + 1} "
                     f"(narrows to {nxt:,})", key="gl_more"):
            st.session_state.gl_revealed += 1
            st.rerun()

    if not st.session_state.gl_solved:
        selected = player_picker("gl_guess", label="Your guess")
        if selected is not None and st.button("Submit guess", key="gl_submit"):
            guess_pid, guess_name = selected
            if guess_pid == target[0]:
                st.session_state.gl_solved = True
            else:
                g = con.execute(
                    f"SELECT {PLAYER_COLS} FROM players WHERE player_id = ?",
                    (guess_pid,)).fetchone()
                st.session_state.gl_guesses.append((guess_name, feedback(target, g)))
            st.rerun()

    if st.session_state.gl_solved:
        st.success(f"Correct — {target[1]}, in "
                   f"{len(st.session_state.gl_guesses) + 1} guess"
                   f"{'es' if st.session_state.gl_guesses else ''} "
                   f"after {revealed} clue{'s' if revealed != 1 else ''}.")

    for guess_name, fb in reversed(st.session_state.gl_guesses):
        with st.expander(f"✗ {guess_name}", expanded=True):
            for k, v in fb.items():
                st.write(f"**{k}:** {v}")

    if not st.session_state.gl_solved and st.button("Give up", key="gl_reveal"):
        st.info(f"It was **{target[1]}** "
                f"({target[2]}–{target[3]}, {target[4]} games, "
                f"{target[8].replace('|', ', ')}).")

    with st.expander("Planned game modes"):
        st.write(
            "Higher or Lower · Guess the match · Name the teammate · "
            "Career path puzzle · Draft and award trivia · Daily challenge")
