"""
obscurity.py -- The sport-agnostic obscurity model.

Obscurity is a 0-100 fame proxy, higher = more obscure, blended from a
handful of ranked career-footprint terms. The machinery here is identical
for every sport; only the *terms* are a sport decision, and each sport
declares its own:

    afl/obscurity_model.py      nba/obscurity_model.py
    mlb/obscurity_model.py      nfl/obscurity_model.py

each exporting a `MODEL`, which sports.py binds to the sport. Nothing
sport-specific belongs in this file -- a term named here would be a term
every sport inherits.

  Term   -- one ranked input: a column (or a derivation) and its weight.
  Model  -- an ordered set of terms plus a version number.
  components(frame, model) -- per-term contributions, blend, confidence.

Scores from different models are not comparable, which is what the version
number on each Model exists to record.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Sequence


# ------------------------------------------------------------ term model

@dataclass(frozen=True)
class Term:
    """
    One ranked contribution to an obscurity score.

    Either `column` names a column on the players frame, or `derive` is a
    callable taking the whole frame and returning a Series -- span needs
    two columns, so a plain column name cannot express it.

    `fillna` is the load-bearing field. A term with `fillna=0.0` treats an
    absent value as a real zero ("kicked no goals"). A term with `fillna=
    None` leaves it NaN, which drops the term for that player and
    renormalises the remaining weights. The difference between "polled no
    votes" and "no vote data exists" is the whole reason this distinction
    is here; see `components`.
    """
    name: str
    weight: float
    column: str = ""
    derive: Optional[Callable] = None
    fillna: Optional[float] = None
    #: Documentation only. The NaN-drop in `components` applies to every
    #: term, not just the ones flagged here -- a NULL final_season drops
    #: `era` by exactly the same path. Flagging a term records that it is
    #: *expected* to be missing for some players rather than a data fault.
    optional: bool = False

    def values(self, frame):
        """This term's raw input, before ranking."""
        series = self.derive(frame) if self.derive else frame[self.column]
        return series if self.fillna is None else series.fillna(self.fillna)


@dataclass(frozen=True)
class Model:
    """An ordered set of terms and the version number they produced."""
    version: int
    terms: Sequence[Term]

    @property
    def weights(self):
        """name -> weight, in declaration order."""
        return {t.name: t.weight for t in self.terms}

    @property
    def optional_terms(self):
        return tuple(t.name for t in self.terms if t.optional)

    def required_columns(self):
        """
        Every players column `components` reads, in declaration order and
        without duplicates. recompute_obscurity.py selects exactly these,
        so a model that grows a new input fails loudly there instead of
        silently scoring against NaN.
        """
        out = []
        for term in self.terms:
            columns = ((term.column,) if term.column
                       else DERIVED_COLUMNS.get(term.derive, ()))
            for column in columns:
                if column and column not in out:
                    out.append(column)
        return out


# --------------------------------------------------------- derived terms

#: Columns a derivation reads, so `Model.required_columns` can report them.
#: Keyed by the function itself rather than by name so a renamed helper
#: cannot quietly stop contributing to the required set.
DERIVED_COLUMNS = {}


def derives(*columns):
    def wrap(fn):
        DERIVED_COLUMNS[fn] = tuple(columns)
        return fn
    return wrap


@derives("debut_season", "final_season")
def span(frame):
    """
    Seasons between debut and final game, which nothing else captures.

    17 games all inside 1899 is a far more obscure career than 17 games
    strung across a decade, and only this term can tell them apart.
    """
    return (frame["final_season"].fillna(frame["debut_season"])
            - frame["debut_season"] + 1).clip(lower=1)


# ------------------------------------------------------------- machinery

def pct_rank_low_is_obscure(s):
    """
    Percentile rank inverted so small values score high, with a tied group
    taking its *best* rank rather than the middle of the tie.

    `method="min"` is the whole reason this scale reaches 100. With pandas'
    default `method="average"`, every member of a tied group takes the
    group's midpoint rank -- and these inputs are mostly ties: 82% of AFL
    players never polled a Brownlow vote, 65% never played a final, 26%
    never kicked a goal. Averaging meant "never polled a vote" scored 58.8
    out of 100 rather than 100, so the most anonymous career possible
    topped out at 84.9 and the top sixth of the scale was unreachable.
    Having none of a thing puts a player in the most anonymous tier for
    that term; how many others share the tier is not evidence about them.
    """
    return (1 - s.rank(pct=True, method="min")) * 100


def components(p, model):
    """
    Per-term obscurity contributions, the blended score, and its confidence.

    Returns a DataFrame with one `<term>_component` column per term, in the
    model's declaration order, plus `obscurity`, `obscurity_confidence` and
    `obscurity_model`. Storing the parts rather than only the total is what
    makes a score auditable: it can be explained in the UI, retuned, and
    compared against an older version.

    Heuristic 0-100 proxy for how unlikely a player is to be picked, where
    higher = more obscure. This is NOT a puzzle's real pick data (which is
    crowd-sourced and not public). It is a fame proxy built from career
    footprint, and should never be presented as measured rarity.

    MISSING IS NOT ZERO
    -------------------
    "Polled no Brownlow votes" and "no Brownlow data exists" are different
    facts, and the first version of the AFL formula read both as the maximum
    obscurity contribution. Brownlow voting starts in 1931, so every one of
    the 3,414 players who finished before it began was being credited for
    failing to poll in a count that did not exist. A term left NaN (that is,
    `fillna=None`) is dropped for that player and the remaining weights are
    renormalised to 1.0, and the player carries a confidence below 1
    recording that a term was unavailable.

    This applies to *every* term, not only the ones marked `optional`. A
    player with a NULL final_season drops the era term by the same path.
    The NBA needs exactly this for minutes, which are not recorded before
    1951-52 -- reading that gap as "played zero minutes" would rank the
    entire pre-war league as maximally obscure for a reason that is an
    artefact of record-keeping.

    ERA
    ---
    Ranked, not clipped. The old AFL term was `(2000 - final_season)` scaled
    and clipped at zero, which made every player who finished from 2000
    onwards equally contemporary -- one 2001 season and a career still
    running in 2026 scored the same. Ranking final seasons keeps the
    ordering (older is more obscure) while restoring separation across the
    last 25 years, and drops two arbitrary constants.
    """
    import pandas as pd

    terms = {}
    for term in model.terms:
        ranked = pct_rank_low_is_obscure(term.values(p))
        if term.fillna is None:
            # Keep NaN where the source has nothing to say, so the blend
            # below drops the term rather than reading it as a real value.
            ranked = ranked.where(term.values(p).notna())
        terms[term.name] = ranked

    weights = model.weights

    # Renormalise over the terms each player actually has. A player missing
    # a 0.10 term is scored on the rest at 1/0.90 their weight, which is the
    # same as saying the missing term would have looked like their average
    # -- the only neutral assumption available.
    available = sum(
        pd.Series(weights[name], index=p.index).where(terms[name].notna(), 0.0)
        for name in terms
    )
    weighted = sum(weights[name] * terms[name].fillna(0.0) for name in terms)

    out = pd.DataFrame(
        {f"{name}_component": value.round(1) for name, value in terms.items()},
        index=p.index)
    out["obscurity"] = (weighted / available).round(1)
    out["obscurity_confidence"] = available.round(3)
    out["obscurity_model"] = model.version
    return out


def score(p, model):
    """The blended 0-100 score alone. See `components`."""
    return components(p, model)["obscurity"]


def disclaimer(model, vocab=None):
    """
    The sentence shown wherever a star rating appears, naming this model's
    actual terms.

    Generated rather than written out because the AFL string went stale:
    it claimed the formula included "club spread", which it never has, and
    the direction is not even obvious -- a player at several clubs
    qualifies for more squares and so may be *easier* to recall. A
    disclaimer that lists terms the model does not use is worse than none.
    """
    names = [TERM_PROSE.get(t.name, t.name) for t in model.terms]
    if vocab is not None:
        names = [n.replace("{score}", vocab.score)
                  .replace("{postseason}", vocab.postseason)
                  .replace("{clubs}", vocab.clubs) for n in names]
    listed = ", ".join(names[:-1]) + " and " + names[-1]
    source = getattr(vocab, "grid_source", None) or "the puzzle"
    return (f"Stars are this database's obscurity proxy, derived from "
            f"{listed} — not the live crowd rarity percentage {source} "
            f"itself reports.")


#: How each term reads in the disclaimer. `{score}`, `{postseason}` and
#: `{clubs}` are filled from the sport's Vocab so the NBA says "points" and
#: "playoffs" where the AFL says "goals" and "finals".
#:
#: Keyed on term name, so a name several sports use is worded once. A term
#: with no entry falls back to its own name, which is why a model is free to
#: add one without touching this table -- "touchdowns" and "hits" read
#: correctly as themselves.
TERM_PROSE = {
    "games": "games played",
    "span": "career span",
    "era": "era",
    "goals": "goals",
    "points": "points",
    "finals": "finals",
    "playoffs": "playoff games",
    "postseason": "{postseason} games",
    "minutes": "minutes played",
    "teams": "number of {clubs}",
    "franchises": "number of {clubs}",
    "home_runs": "home runs",
    "brownlow": "Brownlow votes",
}
