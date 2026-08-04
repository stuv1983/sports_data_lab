#!/usr/bin/env python3
"""
nba/load_nba_playoff_series.py -- Normalise the saved playoff-series extract
into data/nba/reference/playoff_series.csv.

    python -m nba.load_nba_playoff_series
    python -m nba.load_nba_playoff_series --check          # verify, write nothing

WHY THIS IS A REFERENCE FILE AND NOT A SOURCE ADAPTER
------------------------------------------------------
Basketball-Reference is the right tool for verifying a record and the wrong
tool to hang a nightly import on: its terms do not clear a comprehensive
database for redistribution, and a recurring scraper would be a standing
imposition on a site that owes us nothing. So this runs once over HTML that
was already saved, writes a small checked-in CSV, and never touches the
network. `nba/build_nba_db.py` reads the CSV. Nothing in the build ever fetches.

The output is bracket structure -- which teams met, in which round, and who
won -- which the box-score sources do not carry and which four criteria
(Finals appearance, Finals win, championship team, champion) need before
they can answer anything at all.

SEASONS ARE START YEARS HERE, END YEARS THERE
---------------------------------------------
Basketball-Reference labels a series with the year it was *played*: the
2025-26 Finals are `season=2026`. The build uses start years throughout, so
every row is converted on the way in. Getting this backwards silently files
every series one season late, which then matches no games at all.

LEAGUES
-------
The BAA is the NBA's own predecessor and its three championships are NBA
championships -- the league counts them, and so does this. The ABA is a
different league that merged in, and its nine titles are not NBA titles.
Both are written to the reference file with their league recorded; the build
decides what to read. Merging them here would make "NBA champion" wrong in a
way no later check could see.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

import data_paths

#: Where the saved extract lives, relative to the NBA data directory.
EXTRACT = Path("output") / "playoff_series_rows.csv"

#: Round codes, coarsest structure the whole 1946-2026 span shares. The
#: NBA's bracket has been reorganised repeatedly -- divisions before
#: conferences, a bye-heavy 1950s field, a league-wide semi-final in the
#: BAA years -- so these name the *position in the bracket* rather than the
#: era's own label for it.
#:
#:   F    the championship series, every season
#:   CF   the round before it (conference or division finals)
#:   CSF  the round before that
#:   R1   an opening round
#:   QF   a quarter-final in an era that had one as a distinct round
#:   TB   a tiebreak game for seeding, not part of the bracket proper
#:
#: A series whose name is not here is written with an empty round and
#: reported. It is never guessed: an unrecognised series quietly filed as R1
#: would put a Finals game in the first round and cost somebody a title.
ROUND_PATTERNS = (
    (re.compile(r"^Finals$", re.I), "F"),
    (re.compile(r"\bTiebreak\b", re.I), "TB"),
    (re.compile(r"^(?:Eastern|Western|Central) (?:Conf|Div) Finals$", re.I),
     "CF"),
    (re.compile(r"^(?:Eastern|Western|Central) (?:Conf|Div) Semifinals$",
                re.I), "CSF"),
    (re.compile(r"^(?:Eastern|Western|Central) (?:Conf|Div) First Round$",
                re.I), "R1"),
    (re.compile(r"^First Round$", re.I), "R1"),
    (re.compile(r"^Semifinals$", re.I), "CSF"),
    (re.compile(r"^Quarterfinals$", re.I), "QF"),
)

#: The seed Basketball-Reference appends to a team name, as in
#: "Boston Celtics (1)". This is the official seeding, which the box-score
#: sources do not carry -- worth keeping rather than discarding with the
#: rest of the formatting.
SEED = re.compile(r"\s*\((\d+)\)\s*$")

COLUMNS = ("season", "league", "round", "series_name", "winner",
           "winner_seed", "wins_winner", "loser", "loser_seed", "wins_loser",
           "series_url", "source_url")


class ExtractError(RuntimeError):
    """The saved extract is not shaped the way this loader expects."""


def classify(series_name):
    """Round code for a series name, or None if it is not recognised."""
    name = (series_name or "").strip()
    for pattern, code in ROUND_PATTERNS:
        if pattern.search(name):
            return code
    return None


def split_seed(value):
    """('Boston Celtics (1)') -> ('Boston Celtics', 1)."""
    text = (value or "").strip()
    if not text:
        return None, None
    found = SEED.search(text)
    if not found:
        return text, None
    return SEED.sub("", text).strip(), int(found.group(1))


def parse(extract_path):
    """Read the saved extract. Returns (rows, problems)."""
    rows, problems = [], []
    with open(extract_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"season", "lg", "series", "winner", "loser",
                    "wins_winner", "wins_loser"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ExtractError(
                f"{extract_path} is missing column(s) "
                f"{', '.join(sorted(missing))}; the saved page was parsed "
                f"with a different script than this loader expects")

        for line, raw in enumerate(reader, start=2):
            # The extract carries repeated header rows from the source
            # table. They have no season and are skipped, not reported --
            # they are an artefact of the page, not a defect in it.
            season = (raw.get("season") or "").strip()
            if not season.isdigit():
                continue

            name = (raw.get("series") or "").strip()
            code = classify(name)
            if code is None:
                problems.append(
                    f"line {line}: series {name!r} ({season}) matches no "
                    f"known round; written with an empty round")

            winner, winner_seed = split_seed(raw.get("winner"))
            loser, loser_seed = split_seed(raw.get("loser"))
            if not winner or not loser:
                problems.append(
                    f"line {line}: series {name!r} ({season}) names only one "
                    f"team; skipped")
                continue
            if winner == loser:
                problems.append(
                    f"line {line}: series {name!r} ({season}) names "
                    f"{winner!r} on both sides; skipped")
                continue

            rows.append({
                # BBRef's year is the one the series was played in; the
                # build's season is the start year of the season it belongs
                # to. 2026 -> 2025.
                "season": int(season) - 1,
                "league": (raw.get("lg") or "").strip().upper(),
                "round": code or "",
                "series_name": name,
                "winner": winner, "winner_seed": winner_seed,
                "wins_winner": _int(raw.get("wins_winner")),
                "loser": loser, "loser_seed": loser_seed,
                "wins_loser": _int(raw.get("wins_loser")),
                "series_url": (raw.get("series_url") or "").strip(),
                "source_url": (raw.get("source_url") or "").strip(),
            })

    rows.sort(key=lambda r: (r["season"], r["league"], r["round"],
                             r["winner"]))
    return rows, problems


def check(rows):
    """Structural checks. Returns a list of complaints, empty if clean.

    The one that matters is a season with no Finals or with two: it means
    either no champion can be derived or two teams can, and both are worse
    than the build stopping.
    """
    complaints = []
    finals = {}
    pairs = {}
    for row in rows:
        key = (row["season"], row["league"])
        if row["round"] == "F":
            finals.setdefault(key, []).append(row["winner"])
        # A season cannot contain the same matchup twice: a team pair is
        # what the build resolves a game's round by, so a repeat would make
        # the round ambiguous exactly where it is being relied on.
        pair = (key, frozenset((row["winner"], row["loser"])))
        if pair in pairs and row["round"] != "TB":
            complaints.append(
                f"{row['season']} {row['league']}: {row['winner']} and "
                f"{row['loser']} meet in both {pairs[pair]!r} and "
                f"{row['series_name']!r}; a game between them cannot be "
                f"assigned a round")
        pairs[pair] = row["series_name"]

    for key in sorted({(r["season"], r["league"]) for r in rows}):
        won = finals.get(key, [])
        if not won:
            complaints.append(f"{key[0]} {key[1]}: no Finals series")
        elif len(won) > 1:
            complaints.append(
                f"{key[0]} {key[1]}: {len(won)} Finals series "
                f"({', '.join(sorted(won))}); exactly one is required")
    return complaints


def cross_check(rows, leagues_path):
    """Compare derived champions against the league index's own column.

    Two independently parsed pages agreeing is worth a great deal more than
    either alone, and this is the check that would have caught a season
    shifted by one -- the failure mode the start/end year conversion invites.
    """
    if not Path(leagues_path).exists():
        return [f"{leagues_path} not found; champions were not cross-checked"]

    declared = {}
    with open(leagues_path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            label = (raw.get("season") or "").strip()
            champion = (raw.get("champion") or "").strip()
            if "-" not in label or not champion:
                continue
            declared[(int(label[:4]), (raw.get("lg_id") or "").strip().upper())
                     ] = champion

    complaints = []
    derived = {(r["season"], r["league"]): r["winner"]
               for r in rows if r["round"] == "F"}
    for key, winner in sorted(derived.items()):
        expected = declared.get(key)
        if expected is None:
            complaints.append(f"{key[0]} {key[1]}: no champion in the league "
                              f"index to check {winner!r} against")
        elif expected != winner:
            complaints.append(
                f"{key[0]} {key[1]}: the Finals winner is {winner!r} but the "
                f"league index says the champion was {expected!r}")
    for key in sorted(set(declared) - set(derived)):
        complaints.append(f"{key[0]} {key[1]}: the league index has champion "
                          f"{declared[key]!r} but there is no Finals series")
    return complaints


def write(rows, out_path, extract_path, verbose=True):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    # Provenance beside the file, so a later disagreement can be traced to
    # the extract it came from rather than argued about.
    digest = hashlib.sha256(
        Path(extract_path).read_bytes()).hexdigest()
    meta = out_path.with_suffix(".meta.json")
    meta.write_text(json.dumps({
        "generated_by": "nba/load_nba_playoff_series.py",
        "extract": str(extract_path),
        "extract_sha256": digest,
        "source": "https://www.basketball-reference.com/playoffs/series.html",
        "licence": "Basketball-Reference; retained locally for verification "
                   "and bracket structure. Not redistributed.",
        "rows": len(rows),
        "seasons": [min(r["season"] for r in rows),
                    max(r["season"] for r in rows)] if rows else None,
        "leagues": sorted({r["league"] for r in rows}),
    }, indent=2), encoding="utf-8")

    if verbose:
        print(f"{out_path}: {len(rows):,} series")
        print(f"{meta}: extract sha256 {digest[:16]}...")
    return out_path


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.
                                 RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None,
                    help="NBA data directory (default: data/nba)")
    ap.add_argument("--check", action="store_true",
                    help="parse and verify without writing")
    ap.add_argument("--quiet", dest="verbose", action="store_false")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(
        data_paths.default_db("nba")).parent
    extract = root / EXTRACT
    if not extract.exists():
        print(f"No extract at {extract}.", file=sys.stderr)
        return 2

    try:
        rows, problems = parse(extract)
    except ExtractError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not rows:
        print(f"{extract} parsed to no usable rows.", file=sys.stderr)
        return 2

    complaints = (problems + check(rows)
                  + cross_check(rows, root / "output" / "leagues_rows.csv"))
    for complaint in complaints:
        print(f"  {complaint}")

    if args.verbose:
        by_round = {}
        for row in rows:
            by_round[row["round"] or "(unresolved)"] = by_round.get(
                row["round"] or "(unresolved)", 0) + 1
        print(f"{len(rows):,} series, "
              f"{min(r['season'] for r in rows)}-"
              f"{max(r['season'] for r in rows)}")
        for code, count in sorted(by_round.items()):
            print(f"  {code:<12} {count:>4}")

    if args.check:
        return 1 if complaints else 0

    # A complaint is reported but does not block the write: the reference
    # file is also how an unresolved series gets looked at.
    write(rows, root / "reference" / "playoff_series.csv", extract,
          verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
