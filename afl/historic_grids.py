"""
afl/historic_grids.py -- Captured Gridley boards, and what this database can
honestly do with them.

Real grids used to live in a dictionary inside afl/grid_fixtures.py, which
made them visible to the test suite and to nothing else. They are now
declared here as HistoricGrid records so the app, the tests and Game Lab
all read the same list. afl/grid_fixtures.py re-exports it in its old shape
so existing tests keep passing unchanged.

WHAT THIS MODULE PROMISES
-------------------------
Every axis of a historical grid runs through the parser *before* the grid
is offered for play. A criterion that the database cannot answer is
reported as unsupported and named -- it is never quietly swapped for a
different AFL condition that happens to be answerable. CLUB CAPTAIN means
club captain. If captaincy is not in AFL Tables, the square says so.

There are two ways to open a historical grid:

  Authentic mode  A grid is playable only if all six criteria are
                  supported. Grids with unsupported criteria stay
                  visible and selectable, but display why they cannot be
                  played. No substitutions, ever.

  Practice mode   Unsupported criteria are replaced by a supported one of
                  comparable difficulty, and the board says so on the
                  axis itself: "CLUB CAPTAIN — replaced for Practice
                  Mode". The replacement is chosen deterministically and
                  cached in session state so a rerun does not reshuffle
                  the board mid-game.

Practice mode is a different puzzle wearing the same number. That is the
whole reason the label is loud.
"""

import re
from dataclasses import dataclass, field

import labels

from . import constraints as C
from . import parse_criteria as P

MANUAL = "manual_capture"


# --------------------------------------------------------------- records

@dataclass(frozen=True)
class HistoricGrid:
    """
    One captured Gridley board.

    A capture is *complete* when all three rows and all three columns are
    known. Partial captures are kept rather than padded: a grid read off a
    screenshot that only showed part of the board records the criteria it
    actually showed, and `complete` stays False so nothing tries to solve
    nine intersections out of three criteria.
    """
    number: int
    date: str
    rows: tuple = ()
    cols: tuple = ()
    source: str = MANUAL
    #: Criteria expected to be declined. Documented per grid so the test
    #: suite fails if a future parser change starts answering one of them.
    unsupported: tuple = ()
    #: Criteria from an incomplete capture, axis unknown.
    partial_criteria: tuple = ()
    note: str = ""

    @property
    def key(self):
        # A grid typed straight into the app has no Gridley number and no
        # capture date, so it names itself by its source instead of
        # rendering as "#0 ()".
        if not self.number:
            return self.date or self.source.replace("_", " ")
        return f"#{self.number} ({self.date})"

    @property
    def complete(self):
        return len(self.rows) == 3 and len(self.cols) == 3

    @property
    def criteria(self):
        if self.complete:
            return list(self.rows) + list(self.cols)
        return list(self.partial_criteria)


GRIDS = [
    HistoricGrid(
        number=1106, date="2026-07-26",
        cols=("St Kilda", "North Melbourne", "150+ GAMES PLAYED"),
        rows=("30+ GOALS TWO DIFF CLUBS", "MASON WOOD TEAMMATE",
              "NO FINALS WINS"),
    ),
    HistoricGrid(
        number=1107, date="2026-07-27",
        cols=("Gold Coast", "Melbourne", "MCG WON A FINAL"),
        rows=("100+ GOALS CAREER", "CHRISTIAN PETRACCA TEAMMATE",
              "RISING STAR NOMINATION"),
        unsupported=(),
    ),
    HistoricGrid(
        number=1108, date="2026-07-28",
        cols=("St Kilda", "MARVEL STADIUM PLAYED AT", "PLAYED IN 2020s"),
        rows=("4+ GOALS GAME", "WANGANEEN-MILERA TEAMMATE",
              "40+ DISPOSALS IN A GAME"),
    ),
    HistoricGrid(
        number=1109, date="2026-07-29",
        cols=("West Coast", "WOODEN SPOON WINNER", "PLAYED IN A FINAL"),
        rows=("LEADING GOALKICKER TEAM", "JAKE WATERMAN TEAMMATE",
              "BROTHER PLAYED"),
        # BROTHER PLAYED was unsupported when this grid was captured. The
        # broad Wikipedia family layer now answers it, so the criterion is
        # no longer declined and the board is fully playable.
        unsupported=(),
    ),
    HistoricGrid(
        number=1110, date="2026-07-30",
        cols=("Geelong", "Collingwood", "NO GRAND FINALS"),
        rows=("MULTI-CLUB PLAYER", "OLIVER HENRY TEAMMATE",
              "1+ GOAL AVG IN FINALS"),
    ),
    HistoricGrid(
        number=1111, date="2026-07-31",
        cols=("Western Bulldogs", "ALL AUSTRALIAN", "PREMIERSHIP PLAYER"),
        rows=("30+ DISPOSALS & 3+ GOALS GAME", "MARCUS BONTEMPELLI TEAMMATE",
              "CLUB CAPTAIN"),
        unsupported=(),
    ),
    HistoricGrid(
        number=1112, date="2026-08-01",
        cols=("Brisbane Lions", "CARLTON FIRST CAREER GAME",
              "PLAYED IN 2010s"),
        rows=("50+ GAMES TWO DIFF CLUBS", "MITCH ROBINSON TEAMMATE",
              "5+ FINALS GAMES"),
    ),
    HistoricGrid(
        number=1113, date="2026-08-02",
        partial_criteria=("LESS THAN 20 GOALS — CAREER",
                          "WON A FINALS GAME",
                          "AVG 5+ MARKS — SEASON"),
        note="Partial capture: three criteria were legible, and which axis "
             "each sat on was not recorded. Fill in rows/cols when the full "
             "board is available; until then the nine intersections cannot "
             "be checked. The three criteria are still exercised "
             "individually by the test suite.",
    ),
    HistoricGrid(
        number=1114, date="2026-08-03",
        cols=("Port Adelaide", "10+ FINALS GAMES", "GRAND FINAL PLAYER"),
        rows=("30+ FREES AGAINST SEASON", "KANE CORNES TEAMMATE",
              "BROTHER PLAYED"),
    ),
]

BY_NUMBER = {g.number: g for g in GRIDS}
BY_DATE = {g.date: g for g in GRIDS}


def get(number=None, date=None):
    """Look a grid up by Gridley number or by date."""
    if number is not None:
        return BY_NUMBER.get(int(number))
    if date is not None:
        return BY_DATE.get(str(date).strip())
    return None


# -------------------------------------------------------------- analysis

#: Tables that only exist once an optional import step has been run. A
#: criterion whose SQL touches one of these is answerable in principle but
#: not in this database until the loader has run.
OPTIONAL_TABLES = {
    "draft_links": "Draft data — run `afl/load_draftguru.py`, then "
                   "`afl/link_draft.py`.",
    "awards": "Award data — run `afl/load_draftguru.py`, then "
              "`afl/link_people.py`.",
    "all_australian": "All-Australian data — run `afl/load_draftguru.py`, "
                      "then `afl/link_people.py`.",
    "person_links": "Draftguru person links — run `afl/link_people.py`.",
}


@dataclass
class CriterionReport:
    """One axis, after the parser has had a look at it."""
    axis: str                   # "row 1", "column 2", "criterion 3"
    text: str                   # exactly as Gridley wrote it
    label: str | None = None    # this tool's rendering, if supported
    reason: str | None = None   # why it was declined, if it was
    constraint: tuple | None = None
    eligible: int | None = None
    warnings: list = field(default_factory=list)
    #: Set on a Practice-mode board only.
    replaced_from: str | None = None

    @property
    def supported(self):
        return self.constraint is not None

    @property
    def display(self):
        if self.replaced_from:
            return f"{self.label}\n(was: {self.replaced_from} — Practice Mode)"
        return self.label or self.text


@dataclass
class GridReport:
    grid: HistoricGrid
    rows: list = field(default_factory=list)
    cols: list = field(default_factory=list)
    loose: list = field(default_factory=list)   # partial captures
    squares_ok: bool | None = None              # None = not checked
    square_errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def all_criteria(self):
        return self.rows + self.cols + self.loose

    @property
    def total(self):
        return len(self.all_criteria)

    @property
    def supported_count(self):
        return sum(1 for c in self.all_criteria if c.supported)

    @property
    def unsupported(self):
        return [c for c in self.all_criteria if not c.supported]

    @property
    def authentic_playable(self):
        """Authentic mode needs a full board with all six axes answerable."""
        return (self.grid.complete
                and not self.unsupported
                and self.squares_ok is not False)

    @property
    def status(self):
        """The one-line verdict the grid picker shows."""
        if not self.grid.complete:
            return "Partial capture — not playable"
        if self.unsupported:
            missing = ", ".join(
                sorted({_short_reason(c) for c in self.unsupported}))
            return f"{missing} unavailable"
        if self.squares_ok is False:
            return "A square failed to execute"
        return "Ready"

    def line(self):
        """'#1111   5/6 criteria supported   Club captain unavailable'"""
        name = (f"#{self.grid.number}" if self.grid.number
                else self.grid.key)
        return (f"{name}   {self.supported_count}/{self.total} "
                f"criteria supported   {self.status}")


def _short_reason(report):
    """A few words naming what is missing, for the status line."""
    t = report.text.lower()
    if "captain" in t:
        return "Club captain"
    if "rising star" in t:
        return "Rising Star nominations"
    if "brother" in t or "father" in t or "son" in t:
        return "Brother links"
    return report.text.title()


def _optional_warnings(con, sql):
    out = []
    if con is None:
        return out
    present = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, hint in OPTIONAL_TABLES.items():
        if table in sql and table not in present:
            out.append(hint)
    return out



# OPTIONAL_LAYER_AVAILABILITY_V2
def _optional_unavailable_reason(con, sql):
    """Return a user-facing reason when parsed SQL needs unloaded data.

    Parsing proves that the project understands a criterion. Availability is
    a separate database capability: a clean build may not yet contain
    Draftguru awards, draft links or the optional captaincy import.
    """
    lowered = sql.casefold()

    if re.search(r"\bcaptaincies\b", lowered):
        available = getattr(C, "captain_available", None)
        if available is None or not available(con):
            return "Club captain data is not loaded; run `afl/load_captains.py`."

    if re.search(r"\b(?:awards|all_australian|person_links)\b", lowered):
        available = getattr(C, "awards_available", None)
        if available is None or not available(con):
            return ("Award data is not loaded; run `afl/load_draftguru.py`, then "
                    "`afl/link_people.py`.")

    if re.search(r"\b(?:draft|draft_links)\b", lowered):
        available = getattr(C, "draft_available", None)
        if available is None or not available(con):
            return ("Draft data is not loaded; run `afl/load_draftguru.py`, then "
                    "`afl/link_draft.py`.")
    return None

def _era_warnings(sport, sql):
    """Name any stat in this criterion that predates its own recording."""
    if sport is None:
        return []
    out = []
    for stat, first in getattr(sport, "stat_eras", {}).items():
        # \b rather than a substring test: '_' is a word character, so
        # this fires on `marks` and not on `contested_marks`.
        if re.search(rf"\b{re.escape(stat)}\b", sql):
            out.append(f"{labels.words(stat)} was not recorded before "
                       f"{first}.")
    return out


def _report_one(axis, text, con, sport):
    con_sql, label = P.parse(text)
    if con_sql is None:
        return CriterionReport(axis=axis, text=text, reason=label)

    if con is not None:
        unavailable = _optional_unavailable_reason(con, con_sql[0])
        if unavailable:
            return CriterionReport(axis=axis, text=text, label=label,
                                   reason=unavailable)

    rep = CriterionReport(axis=axis, text=text, label=label,
                          constraint=con_sql)
    rep.warnings = (_optional_warnings(con, con_sql[0])
                    + _era_warnings(sport, con_sql[0]))
    # An era caveat is a note about the answer, not a reason to skip it:
    # 40+ DISPOSALS is still countable, it just cannot be satisfied by a
    # pre-1965 player. Only a genuine execution failure -- usually an
    # optional table that has not been imported -- costs the count.
    if con is not None:
        try:
            rep.eligible = C.count(con, [con_sql])
        except Exception as e:
            rep.warnings.append(f"{type(e).__name__}: {e}")
    return rep


def analyse(grid, con=None, sport=None, check_squares=True):
    """
    Run every axis of a grid through the parser before it is loaded.

    With a connection, also count eligible players per criterion and
    execute all nine intersections, so a grid that parses but breaks on
    contact with the database is caught here rather than on the board.
    """
    report = GridReport(grid=grid)

    if grid.complete:
        report.rows = [_report_one(f"row {i}", t, con, sport)
                       for i, t in enumerate(grid.rows, 1)]
        report.cols = [_report_one(f"column {i}", t, con, sport)
                       for i, t in enumerate(grid.cols, 1)]
    else:
        report.loose = [_report_one(f"criterion {i}", t, con, sport)
                        for i, t in enumerate(grid.partial_criteria, 1)]
        if grid.note:
            report.warnings.append(grid.note)

    if con is not None and grid.complete and check_squares:
        if report.unsupported:
            report.squares_ok = None        # nothing to check yet
        else:
            report.squares_ok = True
            for r in report.rows:
                for c in report.cols:
                    try:
                        C.count(con, [r.constraint, c.constraint])
                    except Exception as e:
                        report.squares_ok = False
                        report.square_errors.append(
                            f"{r.label} × {c.label}: {type(e).__name__}: {e}")

    for c in report.all_criteria:
        report.warnings.extend(c.warnings)
    return report


def analyse_all(con=None, sport=None, check_squares=True):
    """Every known grid, analysed. Cheap enough to run at startup."""
    return [analyse(g, con, sport, check_squares) for g in GRIDS]


def supported_grids(reports):
    """Authentic-mode candidates: complete boards with six live axes."""
    return [r for r in reports if r.authentic_playable]


# ------------------------------------------------- practice-mode swaps

#: Supported criteria drawn from Gridley's own observed vocabulary, used
#: only to stand in for something the database cannot answer. Written the
#: way Gridley writes them so a Practice board still reads like a Gridley
#: board.
REPLACEMENT_POOL = (
    "PLAYED IN A FINAL",
    "WON A FINALS GAME",
    "5+ FINALS GAMES",
    "PLAYED A GRAND FINAL",
    "NO GRAND FINALS",
    "PREMIERSHIP PLAYER",
    "NO FINALS WINS",
    "ONE-CLUB PLAYER",
    "MULTI-CLUB PLAYER",
    "3+ CLUBS",
    "150+ GAMES PLAYED",
    "250+ GAMES PLAYED",
    "100+ GOALS CAREER",
    "300+ GOALS CAREER",
    "LESS THAN 20 GOALS — CAREER",
    "LEADING GOALKICKER TEAM",
    "WOODEN SPOON WINNER",
    "ALL AUSTRALIAN",
    "4+ GOALS GAME",
    "30+ DISPOSALS IN A GAME",
    "AVG 5+ MARKS — SEASON",
    "PLAYED IN 2010s",
    "PLAYED IN 2000s",
    "PLAYED IN 1990s",
)


def _target_count(report):
    """
    The difficulty a replacement should aim at.

    An unsupported criterion has no eligible count -- that is precisely
    what "unsupported" means -- so there is nothing to match it against
    directly. The median eligible count of the criteria that *did* parse
    on the same grid is used instead: it keeps the replaced axis in the
    same band as the rest of the board, which is the property that
    actually matters for whether the nine squares stay solvable.
    """
    counts = sorted(c.eligible for c in report.all_criteria
                    if c.supported and c.eligible)
    if not counts:
        return None
    return counts[len(counts) // 2]


def choose_replacement(con, report, criterion, taken=()):
    """
    Pick a supported stand-in of comparable difficulty.

    Deterministic: the same grid and criterion always yield the same
    replacement, so a Streamlit rerun cannot reshuffle a board that is
    half played. `taken` excludes criteria already on this board, so a
    Practice grid never ends up with the same axis twice.
    """
    target = _target_count(report)
    taken = {str(x).strip().lower() for x in taken}
    candidates = []
    for text in REPLACEMENT_POOL:
        if text.strip().lower() in taken:
            continue
        cn, label = P.parse(text)
        if cn is None:
            continue
        try:
            n = C.count(con, [cn])
        except Exception:
            continue
        if n == 0:
            continue
        candidates.append((text, label, cn, n))

    if not candidates:
        return None
    if target is None:
        candidates.sort(key=lambda x: (-x[3], x[0]))
    else:
        # Compare on a ratio, not a difference: 400 vs 200 eligible is the
        # same step in difficulty as 4,000 vs 2,000, and an absolute
        # distance would always pick the largest pool for a large target.
        candidates.sort(key=lambda x: (abs(_ratio(x[3], target)), x[0]))
    text, label, cn, n = candidates[0]

    return CriterionReport(
        axis=criterion.axis, text=text, label=label, constraint=cn,
        eligible=n, replaced_from=criterion.text)


def _ratio(a, b):
    from math import log
    return log(max(a, 1) / max(b, 1))


def practice_board(con, report, cache=None):
    """
    A playable version of a grid whose criteria are not all supported.

    Returns (rows, cols, swaps) where swaps lists (original, replacement)
    pairs for display. `cache` is a plain dict -- pass st.session_state
    (or a slice of it) so replacements survive a rerun.
    """
    if not report.grid.complete:
        return None, None, []

    cache = {} if cache is None else cache
    taken = [c.text for c in report.all_criteria if c.supported]
    swaps = []

    def resolve(criterion):
        if criterion.supported:
            return criterion
        ck = f"practice:{report.grid.number}:{criterion.axis}"
        if ck in cache:
            text = cache[ck]
            cn, label = P.parse(text)
            rep = CriterionReport(
                axis=criterion.axis, text=text, label=label, constraint=cn,
                replaced_from=criterion.text)
            if con is not None and cn is not None:
                rep.eligible = C.count(con, [cn])
        else:
            rep = choose_replacement(con, report, criterion, taken)
            if rep is None:
                return criterion
            cache[ck] = rep.text
        taken.append(rep.text)
        swaps.append((criterion.text, rep.label))
        return rep

    rows = [resolve(c) for c in report.rows]
    cols = [resolve(c) for c in report.cols]
    return rows, cols, swaps
