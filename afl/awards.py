"""
afl/awards.py -- Constraints over the Draftguru award tables.

Same contract as afl/constraints.py: every function returns
(sql_selecting_player_id, params).

Everything here goes through person_links and only trusts rows that
afl/link_people.py resolved. An award row whose person is `ambiguous` or
`unmatched` is invisible to the solver, exactly as ambiguous draft rows
are. That is the difference between "this player was All-Australian" and
"someone with this name was".
"""

# Only these person statuses are trusted.
_OK = "l.match_status IN ('from_draft','unique','resolved') " \
      "AND l.player_id IS NOT NULL"

# Award slugs as Draftguru names them, grouped for the UI.
AWARD_SLUGS = {
    "All-Australian squad": "all-australian-squad",
    "Brownlow Medal": "brownlow-medal",
    "Coleman Medal": "coleman",
    "Norm Smith Medal": "norm-smith-medal",
    "Rising Star winner": "rising-star",
    "AFLCA Champion Player": "aflca-champion",
    "AFLCA Best Young Player": "aflca-best-young-player",
    "Gary Ayres Award": "gary-ayres-award",
    "AFLPA MVP": "aflpa-mvp",
    "AFLPA Best First-Year Player": "aflpa-best-first-year-player",
    "Magarey Medal (SANFL)": "magarey-medal",
    "Sandover Medal (WAFL)": "sandover-medal",
    "Liston Trophy (VFL)": "liston-trophy",
    "Morrish Medal (u18)": "morrish-medal",
    "Larke Medal (u18)": "larke-medal",
    "Hunter Harrison Medal": "hunter-harrison-medal",
    "Gardiner Medal": "gardiner-medal",
    "Geoff Christian Medal": "geoff-christian-medal",
    "National Draft pick #1": "national_draft_pick_1",
}

STATE_LEAGUE = ["magarey-medal", "sandover-medal", "liston-trophy",
                "morrish-medal", "larke-medal", "hunter-harrison-medal",
                "gardiner-medal", "geoff-christian-medal"]


def _award(where, params):
    return (f"""SELECT DISTINCT l.player_id
                FROM awards a JOIN person_links l
                  ON l.dg_person_id = a.dg_person_id
                WHERE {_OK} AND {where}""", params)


# ----------------------------------------------------------- All-Australian

_AA_HISTORY_PLACEHOLDER = """
CREATE TEMP TABLE IF NOT EXISTS all_australian_history (
    season INTEGER, player_id INTEGER, match_status TEXT
)
"""


def ensure_all_australian_history_table(con):
    exists = con.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='all_australian_history'"
    ).fetchone()
    if not exists:
        # The app deliberately uses a mode=ro connection with query_only set.
        # The URI protects the database file, while query_only also blocks the
        # connection-local TEMP placeholder needed by older databases.  Toggle
        # only long enough to create that empty TEMP table, then restore the
        # original setting; mode=ro continues to prevent persistent writes.
        query_only = bool(con.execute("PRAGMA query_only").fetchone()[0])
        if query_only:
            con.execute("PRAGMA query_only = OFF")
        try:
            con.execute(_AA_HISTORY_PLACEHOLDER)
        finally:
            if query_only:
                con.execute("PRAGMA query_only = ON")

def all_australian(times=1):
    """Named in at least N All-Australian teams."""
    return ("""SELECT player_id FROM (
                   SELECT l.player_id, t.season
                   FROM all_australian t JOIN person_links l
                     ON l.dg_person_id = t.dg_person_id
                   WHERE l.match_status IN ('from_draft','unique','resolved')
                     AND l.player_id IS NOT NULL
                   UNION
                   SELECT player_id, season FROM all_australian_history
                   WHERE match_status IN ('unique','resolved')
                     AND player_id IS NOT NULL
               ) GROUP BY player_id
               HAVING COUNT(DISTINCT season) >= ?""", [times])


def all_australian_captain():
    return ("""SELECT DISTINCT l.player_id
               FROM all_australian t JOIN person_links l
                 ON l.dg_person_id = t.dg_person_id
               WHERE l.match_status IN ('from_draft','unique','resolved')
                 AND l.player_id IS NOT NULL AND t.is_captain = 1""", [])


def all_australian_in_position(position):
    return ("""SELECT DISTINCT l.player_id
               FROM all_australian t JOIN person_links l
                 ON l.dg_person_id = t.dg_person_id
               WHERE l.match_status IN ('from_draft','unique','resolved')
                 AND l.player_id IS NOT NULL
                 AND LOWER(t.position) LIKE ?""", [f"%{position.lower()}%"])


#: Line groups over the position codes Draftguru's team sheets use.
#: Explicit code sets, not substring matches: 'FF' is inside 'HFF' and a
#: LIKE would quietly hand full-forward squares the half-forward flank.
AA_POSITION_GROUPS = {
    "forward": ("FF", "CHF", "FP", "HFF"),
    "back": ("FB", "CHB", "BP", "HBF"),
    "midfield": ("C", "W", "RR", "Ro"),
    "ruck": ("Ru",),
    "interchange": ("IC",),
}

AA_POSITION_CHOICES = tuple(
    (key, key.capitalize()) for key in AA_POSITION_GROUPS)


def all_australian_position_group(group):
    """Named All-Australian in one line of the ground -- "ALL AUSTRALIAN
    FORWARD". Positions are recorded from 1991 on; interchange picks are a
    separate group, matching Gridley's own exclusion of them."""
    try:
        codes = AA_POSITION_GROUPS[str(group).strip().lower()]
    except KeyError:
        raise ValueError(f"unknown All-Australian line: {group!r}") from None
    marks = ",".join("?" for _ in codes)
    return (f"""SELECT DISTINCT l.player_id
                FROM all_australian t JOIN person_links l
                  ON l.dg_person_id = t.dg_person_id
                WHERE l.match_status IN ('from_draft','unique','resolved')
                  AND l.player_id IS NOT NULL
                  AND t.position IN ({marks})""", list(codes))


def all_australian_between(lo, hi):
    return ("""SELECT DISTINCT player_id FROM (
                   SELECT l.player_id, t.season
                   FROM all_australian t JOIN person_links l
                     ON l.dg_person_id = t.dg_person_id
                   WHERE l.match_status IN ('from_draft','unique','resolved')
                     AND l.player_id IS NOT NULL
                   UNION
                   SELECT player_id, season FROM all_australian_history
                   WHERE match_status IN ('unique','resolved')
                     AND player_id IS NOT NULL
               ) WHERE season BETWEEN ? AND ?""", [lo, hi])


# -------------------------------------------------------------- named awards

def won_award(slug):
    """Appears on a given Draftguru award page."""
    return _award("a.award_slug = ?", [slug])


def won_award_times(slug, times=2):
    return (f"""SELECT l.player_id
                FROM awards a JOIN person_links l
                  ON l.dg_person_id = a.dg_person_id
                WHERE {_OK} AND a.award_slug = ?
                GROUP BY l.player_id HAVING COUNT(DISTINCT a.season) >= ?""",
            [slug, times])


def brownlow_medallist():
    return won_award("brownlow-medal")


def coleman_medallist():
    return won_award("coleman")


def norm_smith_medallist():
    return won_award("norm-smith-medal")


def all_australian_squad():
    return won_award("all-australian-squad")


def number_one_draft_pick():
    return won_award("national_draft_pick_1")


def state_league_medallist():
    marks = ",".join("?" * len(STATE_LEAGUE))
    return (f"""SELECT DISTINCT l.player_id
                FROM awards a JOIN person_links l
                  ON l.dg_person_id = a.dg_person_id
                WHERE {_OK} AND a.award_slug IN ({marks})""", list(STATE_LEAGUE))


# ------------------------------------------------------ club best-and-fairest

def best_and_fairest(times=1):
    return (f"""SELECT l.player_id
                FROM awards a JOIN person_links l
                  ON l.dg_person_id = a.dg_person_id
                WHERE {_OK} AND a.source_category = 'club_best_and_fairest'
                GROUP BY l.player_id
                HAVING COUNT(DISTINCT a.season || ':' || a.award_slug) >= ?""",
            [times])


def best_and_fairest_at(club):
    """Won a club's best-and-fairest, respecting historical identities."""
    c = club.strip().lower()

    # Draftguru combines the Bears and Lions under ``Brisbane``. Split the
    # result by season so choosing one identity cannot leak winners from the
    # other. Its Sydney page already labels 1980-81 as South Melbourne, and
    # Gridley treats that lineage as Sydney, so include both names there.
    if c == "brisbane bears":
        return _award(
            "a.source_category = 'club_best_and_fairest' "
            "AND LOWER(a.club) = 'brisbane' AND a.season <= 1996", [])
    if c == "brisbane lions":
        return _award(
            "a.source_category = 'club_best_and_fairest' "
            "AND LOWER(a.club) = 'brisbane' AND a.season >= 1997", [])
    if c in {"sydney", "sydney swans", "south melbourne"}:
        return _award(
            "a.source_category = 'club_best_and_fairest' "
            "AND LOWER(a.club) IN ('sydney','south melbourne')", [])

    slug = c.replace(" ", "_")
    return _award(
        "a.source_category = 'club_best_and_fairest' "
        "AND (LOWER(a.award_slug) = ? OR LOWER(a.club) = ?)",
        [slug, c])


def best_and_fairest_at_multiple_clubs(clubs=2):
    return (f"""SELECT l.player_id
                FROM awards a JOIN person_links l
                  ON l.dg_person_id = a.dg_person_id
                WHERE {_OK} AND a.source_category = 'club_best_and_fairest'
                GROUP BY l.player_id
                HAVING COUNT(DISTINCT a.award_slug) >= ?""", [clubs])


# ------------------------------------------------------------------ registry

AWARD_BUILDERS = {
    "All-Australian":               (all_australian, ["times"]),
    "All-Australian captain":       (all_australian_captain, []),
    "All-Australian between years": (all_australian_between, ["from", "to"]),
    "All-Australian squad":         (all_australian_squad, []),
    "All-Australian in a position": (all_australian_position_group,
                                     ["aa_position"]),
    "Won an award…":                (won_award, ["award"]),
    "Won an award X+ times":        (won_award_times, ["award", "times"]),
    "Brownlow Medallist":           (brownlow_medallist, []),
    "Coleman Medallist":            (coleman_medallist, []),
    "Norm Smith Medallist":         (norm_smith_medallist, []),
    "State-league medallist":       (state_league_medallist, []),
    "Number one draft pick":        (number_one_draft_pick, []),
    "Club best and fairest":        (best_and_fairest, ["times"]),
    "Club best and fairest at…":    (best_and_fairest_at, ["club"]),
    "B&F at 2+ clubs":              (best_and_fairest_at_multiple_clubs,
                                     ["clubs"]),
}


def awards_available(con):
    """True when required columns and at least one trusted link are present."""
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"awards", "all_australian", "person_links"} <= have:
        return False
    required = {
        "awards": {"award_slug", "source_category", "season", "dg_person_id"},
        "all_australian": {"season", "is_captain", "dg_person_id"},
        "person_links": {"dg_person_id", "player_id", "match_status"},
    }
    for table, columns in required.items():
        present = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if not columns <= present:
            return False
    return bool(con.execute(
        "SELECT 1 FROM awards a JOIN person_links l "
        "ON l.dg_person_id=a.dg_person_id "
        "WHERE l.match_status IN ('from_draft','unique','resolved') "
        "AND l.player_id IS NOT NULL "
        "UNION ALL "
        "SELECT 1 FROM all_australian t JOIN person_links l "
        "ON l.dg_person_id=t.dg_person_id "
        "WHERE l.match_status IN ('from_draft','unique','resolved') "
        "AND l.player_id IS NOT NULL LIMIT 1"
    ).fetchone())


# --------------------------------------------------- draft signing squares
# Draftguru's `Signing` column records how a player arrived, as
# "Father-Son ( Anthony Daniher )", "Academy (GWS)", "Zone (Gold Coast)",
# "FA (Restricted)", "International" and so on. afl/load_draftguru.py splits
# that into signing_kind ("Father-Son") and signing_detail ("Anthony
# Daniher"), so these match the kind exactly rather than by substring.

def _draft(where, params):
    return (f"""SELECT DISTINCT l.player_id
                FROM draft d JOIN person_links l
                  ON l.dg_person_id = d.dg_person_id
                WHERE {_OK} AND {where}""", params)


def signing_type(kind):
    return _draft("LOWER(d.signing_kind) = LOWER(?)", [kind])


def father_son():
    """Recruited under the father-son rule -- so their father played."""
    return signing_type("Father-Son")


def academy_selection():
    return signing_type("Academy")


def national_draft_pick_between(lo, hi):
    """
    Top-N pick, National Draft only.

    Pick numbers restart for each draft type on a Draftguru year page, so
    an unqualified `pick BETWEEN 1 AND 10` sweeps in rookie and pre-season
    selections numbered 1-10 as well.
    """
    return _draft("d.draft_kind = 'national' "
                  "AND d.pick BETWEEN ? AND ?", [lo, hi])


AWARD_BUILDERS.update({
    "Father-son selection":       (father_son, []),
    "Academy selection":          (academy_selection, []),
})
