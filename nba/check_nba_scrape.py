#!/usr/bin/env python3
"""
nba/check_nba_scrape.py -- Is the Basketball-Reference scrape ready to build?

    python -m nba.check_nba_scrape
    python -m nba.check_nba_scrape --root C:/nbaData/data
    python -m nba.check_nba_scrape --seasons 2016-2024 --verbose

Reads the scrape the way `nba/build_nba_db.py --source bbr` will read it and
reports what it finds, without building anything. Run it before a long
build rather than discovering an hour in that a third of the box scores
have not landed.

WHAT IT CHECKS
--------------
Coverage      how many indexed games have a box score on disk, overall and
              per season -- a season at 40% will produce career totals that
              are wrong rather than merely incomplete
The contract  every frame the build asks for, validated through
              nba_source.validate, so a renamed scraper column fails here
              in a second instead of at minute forty of a build
Playoffs      whether reference/playoff_series.csv covers the seasons with
              post-season games. Without it no round is assigned, no
              champion is derived, and every Finals and championship square
              silently answers nobody
Dates         that the schedule's dates parse, since the scraper currently
              writes the game key into `game_date`

Exit status is 0 when a build would work, 1 when it would produce a
database with holes worth knowing about first, and 2 when it would fail.
"""

import argparse
import sys
from pathlib import Path

import data_paths
from . import nba_source
from . import nba_source_bbr

#: A season below this share of box scores makes career and per-season
#: totals misleading rather than incomplete: a player's "career points"
#: silently omits the games that have not landed. Reported, not enforced.
THIN_SEASON = 0.95


def check(root, seasons=None, leagues=nba_source_bbr.NBA_LINEAGE,
          verbose=False):
    """Report on a scrape root. Returns (exit_status, lines)."""
    lines, worst = [], 0

    def say(text=""):
        lines.append(text)

    try:
        source = nba_source_bbr.BbrNbaSource(root, leagues=leagues,
                                            verbose=False)
        found = source.coverage()
    except nba_source.SourceError as exc:
        return 2, [f"FAIL  {exc}"]

    say(f"root          {source.root}")
    say(f"seasons       {found['first_season']}-{found['last_season']} "
        f"({len(found['seasons'])} season(s))")
    say(f"games indexed {found['games']:,}")
    say(f"box scores    {found['boxscores']:,} "
        f"({found['percent']}%), {found['missing']:,} still to come")
    for note in source.notes():
        say(f"  note: {note}")
    say()

    wanted = sorted(seasons) if seasons else found["seasons"]

    # -- the contract, on one season, before anything expensive ---------
    try:
        source.teams()
        source.players()
    except nba_source.SourceError as exc:
        return 2, lines + [f"FAIL  the source contract is not met: {exc}"]

    # -- per season -----------------------------------------------------
    index = source._boxscore_index()          # noqa: SLF001 -- same package
    by_season = {}
    for row in source._schedule():            # noqa: SLF001
        season = row["_season"]
        slot = by_season.setdefault(season, {"games": 0, "boxscores": 0,
                                             "playoff": 0, "undated": 0})
        slot["games"] += 1
        if (row.get("bbr_game_key") or "").strip() in index:
            slot["boxscores"] += 1
        if row["_phase"] == "playoff":
            slot["playoff"] += 1
        if not row["_date"]:
            slot["undated"] += 1

    missing_seasons = [s for s in wanted if s not in by_season]
    if missing_seasons:
        worst = max(worst, 1)
        say(f"WARN  {len(missing_seasons)} requested season(s) are not in "
            f"the index: {_span(missing_seasons)}")

    thin = []
    for season in wanted:
        slot = by_season.get(season)
        if not slot:
            continue
        share = slot["boxscores"] / slot["games"] if slot["games"] else 0.0
        if share < THIN_SEASON:
            thin.append((season, slot["boxscores"], slot["games"]))
        if verbose:
            say(f"  {_label(season)}  {slot['boxscores']:>5,}/"
                f"{slot['games']:<5,} box scores  "
                f"{slot['playoff']:>3,} playoff")
    if thin:
        worst = max(worst, 1)
        say(f"WARN  {len(thin)} season(s) are not fully scraped; career "
            f"totals will be short until they are:")
        for season, have, total in thin[:8]:
            say(f"        {_label(season)}  {have:,}/{total:,}")
        if len(thin) > 8:
            say(f"        ... and {len(thin) - 8} more")
        say("      build these with --complete-only for an honest partial "
            "database, or wait.")

    undated = sum(s["undated"] for s in by_season.values())
    if undated:
        worst = max(worst, 1)
        say(f"WARN  {undated:,} game(s) have no parseable date; they are "
            f"left out of the build")

    # -- playoffs -------------------------------------------------------
    from . import nba_playoff_rounds
    db_root = Path(data_paths.default_db("nba")).resolve().parent
    rounds = nba_playoff_rounds.load(db_root)
    playoff_seasons = {s for s, slot in by_season.items()
                       if slot["playoff"] and s in set(wanted)}
    if not rounds:
        if playoff_seasons:
            worst = max(worst, 1)
            say(f"WARN  no {db_root / nba_playoff_rounds.REFERENCE}. "
                f"{len(playoff_seasons)} season(s) have playoff games and "
                f"none would get a round, so no champion is derived and "
                f"every Finals square answers nobody. "
                f"Run nba/load_nba_playoff_series.py first.")
    else:
        covered = {season for season, _ in rounds}
        uncovered = sorted(playoff_seasons - covered)
        if uncovered:
            worst = max(worst, 1)
            say(f"WARN  the playoff-series reference does not cover "
                f"{len(uncovered)} season(s) with playoff games: "
                f"{_span(uncovered)}")
        elif playoff_seasons:
            say(f"ok    playoff-series reference covers all "
                f"{len(playoff_seasons)} post-season(s)")

    say()
    if worst == 0:
        say("READY  build with:")
    else:
        say("BUILDABLE, with the gaps above:")
    say(f"       python -m nba.build_nba_db --source bbr "
        f"--source-root {source.root}")
    return worst, lines


def _label(season):
    return f"{int(season)}-{(int(season) + 1) % 100:02d}"


def _span(seasons):
    seasons = sorted(seasons)
    if len(seasons) <= 6:
        return ", ".join(str(s) for s in seasons)
    return f"{seasons[0]}-{seasons[-1]} ({len(seasons)} of them)"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None,
                    help="the scrape's data directory (default: "
                         "$NBA_SCRAPE_ROOT or data/nba/raw/bbr)")
    ap.add_argument("--seasons", default=None,
                    help="only check these, e.g. 2016-2024")
    ap.add_argument("--include-aba", action="store_true",
                    help="count ABA seasons too")
    ap.add_argument("--verbose", action="store_true",
                    help="one line per season")
    args = ap.parse_args(argv)

    seasons = (nba_source.parse_seasons(args.seasons)
               if args.seasons else None)
    status, lines = check(
        args.root, seasons=seasons,
        leagues=None if args.include_aba else nba_source_bbr.NBA_LINEAGE,
        verbose=args.verbose)
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
