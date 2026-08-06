#!/usr/bin/env python3
"""
afl/build_db.py -- One-time (or occasional) build of the local Gridley database.

Pulls the community-maintained cached copy of the AFL Tables player-stats
dataset from GitHub (the same file the R package `fitzRoy` uses), normalises
it, and writes a local SQLite database.

This deliberately does NOT scrape afltables.com directly:
  * afltables.com/robots.txt disallows automated clients on the stats paths.
  * The fitzRoy_data mirror is a single ~14 MB file that is refreshed by its
    maintainers, so one download replaces tens of thousands of page requests.

Usage:
    python -m afl.build_db                  # build ./gridley.db
    python -m afl.build_db --refresh        # re-download even if cached
    python -m afl.build_db --db my.db
    python -m afl.build_db --no-matches     # skip the afl/derive_matches.py step
    python -m afl.build_db --allow-duplicates   # build despite unresolved dupes
"""

import argparse
import os
import sys
import sqlite3
import urllib.request
from pathlib import Path

import obscurity
from data_paths import default_db

from . import obscurity_model

DATA_URL = (
    "https://github.com/jimmyday12/fitzRoy_data/raw/main/"
    "data-raw/afl_tables_playerstats/afldata.rda"
)
CACHE = os.path.join("data", "afl", "raw", "afldata.rda")
if not os.path.exists(CACHE) and os.path.exists("afldata.rda"):
    CACHE = "afldata.rda"          # legacy pre-reorganisation location

# Historical club name -> the identity Gridley normally uses for "current" club.
CLUB_LINEAGE = {
    "Brisbane Bears": "Brisbane Bears",   # kept distinct: Gridley treats
    "Brisbane Lions": "Brisbane Lions",   # Bears and Lions as separate squares
    "Kangaroos": "North Melbourne",
    "North Melbourne": "North Melbourne",
    "South Melbourne": "Sydney",
    "Sydney": "Sydney",
    "Footscray": "Western Bulldogs",
    "Western Bulldogs": "Western Bulldogs",
    "Greater Western Sydney": "GWS",
    "GWS": "GWS",
}

STAT_COLS = {
    "Kicks": "kicks",
    "Marks": "marks",
    "Handballs": "handballs",
    "Disposals": "disposals",
    "Goals": "goals",
    "Behinds": "behinds",
    "Hit.Outs": "hitouts",
    "Tackles": "tackles",
    "Rebounds": "rebounds",
    "Inside.50s": "inside50s",
    "Clearances": "clearances",
    "Clangers": "clangers",
    "Frees.For": "frees_for",
    "Frees.Against": "frees_against",
    "Brownlow.Votes": "brownlow",
    "Contested.Possessions": "contested",
    "Uncontested.Possessions": "uncontested",
    "Contested.Marks": "contested_marks",
    "Marks.Inside.50": "marks_i50",
    "One.Percenters": "one_percenters",
    "Bounces": "bounces",
    "Goal.Assists": "goal_assists",
}


def download(refresh=False):
    if os.path.exists(CACHE) and not refresh:
        mb = os.path.getsize(CACHE) / 1e6
        print(f"Using cached {CACHE} ({mb:.1f} MB). Use --refresh to re-download.")
        return CACHE
    print(f"Downloading {DATA_URL} ...")
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    urllib.request.urlretrieve(DATA_URL, CACHE)
    print(f"Saved {CACHE} ({os.path.getsize(CACHE)/1e6:.1f} MB)")
    return CACHE


def load_frame(path):
    try:
        import pyreadr
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install pyreadr pandas")
    print("Reading R data file (this takes ~20s) ...")
    return pyreadr.read_r(path)["afldata"]


def _row_signature(frame, index):
    """Every column of one row, as a comparable tuple.

    Used to tell an exact duplicate (the same row recorded twice, safe to
    collapse) from a conflicting one (two different rows sharing a key,
    which needs sequence evidence to settle).
    """
    return tuple(frame.loc[index].tolist())


def _deduplicate_games(out, strict=True):
    """Drop duplicate (player, date) rows. Returns (frame, unresolved).

    Resolution is delegated to dedupe_games, which uses career_game_no
    continuity; this function only handles the DataFrame plumbing and the
    build-level policy on what to do with what it could not settle.
    """
    import pandas as pd

    import dedupe_games

    duplicated = out.duplicated(subset=["player_id", "date"], keep=False)
    if not duplicated.any():
        return out, []

    affected = out.loc[duplicated]
    print(f"{len(affected):,} rows share a (player, date) key with another -- "
          f"resolving before career totals are taken")

    # Only the affected players need the full row treatment; building a
    # signature for all 693k rows to inspect a handful would be wasteful.
    subset = out.loc[out["player_id"].isin(affected["player_id"].unique())]
    rows = [
        {
            "player_id": player_id,
            "date": date,
            # NaN means the source never numbered this appearance, which is
            # no evidence at all -- pass it through as None so the resolver
            # declines rather than comparing against NaN.
            "career_game_no": (None if pd.isna(career_game_no)
                               else career_game_no),
            "ref": index,
            "fields": _row_signature(subset, index),
        }
        for index, player_id, date, career_game_no in zip(
            subset.index, subset["player_id"], subset["date"],
            subset["career_game_no"])
    ]

    drop, unresolved = dedupe_games.resolve(rows)
    if drop:
        print(f"  resolved {len(drop):,} duplicate row(s) by career sequence")
        out = out.drop(index=drop)

    if unresolved:
        print(f"  {len(unresolved):,} duplicate key(s) could NOT be resolved:")
        for item in unresolved:
            print(f"    player {item.player_id} on {item.date}: "
                  f"career_game_no candidates {item.candidates} "
                  f"-- {item.reason}")
        if strict:
            sys.exit(
                "Unresolved duplicate player-game rows: career totals built "
                "from this source would be wrong. Re-run with "
                "--allow-duplicates to build anyway (career_games will be "
                "inflated for the players listed above).")

    return out, unresolved


def build(db_path, refresh=False, skip_matches=False, strict=True):
    import pandas as pd

    df = load_frame(download(refresh))
    print(f"Loaded {len(df):,} player-game rows, {df.ID.nunique():,} players")

    out = pd.DataFrame()
    out["player_id"] = df["ID"].astype("Int64")
    out["player"] = df["Player"].astype(str).str.strip()
    out["season"] = df["Season"].astype(int)
    out["round"] = df["Round"].astype(str)
    out["date"] = df["Date"].astype(str)
    out["venue"] = df["Venue"].astype(str)
    out["club_hist"] = df["Playing.for"].astype(str).str.strip()
    out["club_now"] = out["club_hist"].map(lambda c: CLUB_LINEAGE.get(c, c))
    out["career_game_no"] = pd.to_numeric(df["Career.Games"], errors="coerce")
    out["dob"] = df["DOB"].astype(str)

    # DOB is populated for only ~5% of rows, but Age (fractional years, at
    # that match) and Date are complete. Age is fractional, not integer, so
    # the birth date is recoverable precisely: validated against the 37,544
    # rows that do carry a DOB, median error is 0 days and 99.94% land in
    # the correct calendar year. The +/-1yr birthday ambiguity therefore
    # applies to Draftguru's *integer* ages, not to this side.
    age = pd.to_numeric(df["Age"], errors="coerce")
    match_date = pd.to_datetime(df["Date"], errors="coerce")
    out["birth_est"] = match_date - pd.to_timedelta(age * 365.25, unit="D")
    out["birth_year_est"] = out["birth_est"].dt.year

    for src, dst in STAT_COLS.items():
        out[dst] = pd.to_numeric(df[src], errors="coerce")

    # Opponent and result, derived from the home/away fields.
    # NOTE: this dataset exposes Home.score / Away.score. It does NOT have
    # Home.Points / Away.Points. Assert before relying on them.
    required = ["Home.team", "Away.team", "Home.Away", "Home.score",
                "Away.score", "ID", "Season", "Playing.for"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        sys.exit(f"Source data is missing expected columns: {missing}\n"
                 f"Available: {sorted(df.columns)}")

    home, away = df["Home.team"].astype(str), df["Away.team"].astype(str)
    is_home = df["Home.Away"].astype(str).str.lower().str.startswith("h")
    out["opponent"] = away.where(is_home, home)

    # Persist the home/away flag. It used to be a local only, which meant a
    # match could be reconstructed as a *pair* of clubs from the player rows
    # but not oriented -- and venue is not a reliable substitute (neutral
    # grounds, and the MCG/Docklands tenancies where both sides are hosts).
    # afl/derive_matches.py needs this to fill home_team/away_team properly.
    out["is_home"] = is_home.astype(int)

    hs = pd.to_numeric(df["Home.score"], errors="coerce")
    aws = pd.to_numeric(df["Away.score"], errors="coerce")
    points_for = hs.where(is_home, aws)
    points_against = aws.where(is_home, hs)

    out["result"] = "D"
    out.loc[points_for > points_against, "result"] = "W"
    out.loc[points_for < points_against, "result"] = "L"
    out.loc[points_for.isna() | points_against.isna(), "result"] = None
    out["points_for"] = points_for
    out["points_against"] = points_against

    n_res = out["result"].notna().sum()
    print(f"Derived results for {n_res:,} of {len(out):,} rows "
          f"({out['result'].value_counts().to_dict()})")

    # Finals rounds on AFL Tables are named, not numbered.
    finals = {"EF", "QF", "SF", "PF", "GF"}
    out["is_final"] = out["round"].str.upper().str.strip().isin(finals).astype(int)

    out = out.dropna(subset=["player_id"])

    # Duplicate player-game rows have to go before the groupby below, because
    # career_games is a row count: one stray row is one phantom appearance,
    # and every derived figure inherits it. See dedupe_games for why they
    # cannot simply be dropped on sight.
    out, unresolved = _deduplicate_games(out, strict=strict)

    print("Aggregating career records ...")
    g = out.groupby("player_id")
    players = pd.DataFrame({
        "player": g["player"].first(),
        "dob": g["dob"].first(),
        "birth_year": g["birth_year_est"].median().round(),
        "birth_year_min": g["birth_year_est"].min(),
        "birth_year_max": g["birth_year_est"].max(),
        "debut_season": g["season"].min(),
        "final_season": g["season"].max(),
        "career_games": g.size(),
        "career_goals": g["goals"].sum(min_count=1),
        "career_brownlow": g["brownlow"].sum(min_count=1),
        "finals_played": g["is_final"].sum(),
        "best_disposals": g["disposals"].max(),
        "best_goals": g["goals"].max(),
        "clubs_hist": g["club_hist"].apply(lambda s: "|".join(sorted(set(s)))),
        "clubs_now": g["club_now"].apply(lambda s: "|".join(sorted(set(s)))),
        "n_clubs": g["club_now"].nunique(),
    }).reset_index()

    # The components go in alongside the score so the UI can explain a
    # rating and a formula change can be diffed against the stored one.
    for column, values in obscurity_components(players).items():
        players[column] = values

    from names import normalise_name
    players["name_key"] = players["player"].map(normalise_name)

    dupes = players["name_key"].duplicated(keep=False).sum()
    print(f"{dupes:,} players share a name with at least one other "
          f"(name alone is not a safe identifier)")

    out["birth_est"] = out["birth_est"].dt.strftime("%Y-%m-%d")

    print(f"Writing {db_path} ...")
    con = sqlite3.connect(db_path)
    out.to_sql("games", con, if_exists="replace", index=False)
    players.to_sql("players", con, if_exists="replace", index=False)

    # --- team_seasons: home-and-away ladder, for wooden spoons ------------
    ha = out[out["is_final"] == 0]
    matches = ha.drop_duplicates(subset=["season", "club_now", "date"])
    ts = matches.groupby(["season", "club_now"]).agg(
        played=("result", "size"),
        wins=("result", lambda s: (s == "W").sum()),
        draws=("result", lambda s: (s == "D").sum()),
        losses=("result", lambda s: (s == "L").sum()),
        points_for=("points_for", "sum"),
        points_against=("points_against", "sum"),
    ).reset_index()
    ts["premiership_points"] = ts["wins"] * 4 + ts["draws"] * 2
    ts["percentage"] = (ts["points_for"] /
                        ts["points_against"].replace(0, pd.NA) * 100)
    ts["ladder_rank"] = ts.groupby("season")[
        ["premiership_points", "percentage"]].rank(
        ascending=False, method="min").mean(axis=1)
    ts["ladder_rank"] = ts.groupby("season")["ladder_rank"].rank(method="min")
    teams_in_season = ts.groupby("season")["club_now"].transform("size")
    ts["wooden_spoon"] = (ts["ladder_rank"] == teams_in_season).astype(int)
    ts.to_sql("team_seasons", con, if_exists="replace", index=False)
    print(f"team_seasons: {len(ts):,} rows, "
          f"{int(ts.wooden_spoon.sum())} wooden spoons")

    # --- season_goals: per player per club per season, leading goalkicker -
    sg = out.groupby(["player_id", "season", "club_now"], as_index=False).agg(
        goals=("goals", "sum"), games=("goals", "size"))
    best = sg.groupby(["season", "club_now"])["goals"].transform("max")
    sg["is_club_leading"] = ((sg["goals"] == best) & (sg["goals"] > 0)).astype(int)
    sg.to_sql("season_goals", con, if_exists="replace", index=False)
    print(f"season_goals: {len(sg):,} rows, "
          f"{int(sg.is_club_leading.sum()):,} club-leading seasons")

    cur = con.cursor()
    for stmt in [
        "CREATE INDEX IF NOT EXISTS ix_games_player ON games(player_id)",
        "CREATE INDEX IF NOT EXISTS ix_games_club ON games(club_now)",
        "CREATE INDEX IF NOT EXISTS ix_games_season ON games(season)",
        "CREATE INDEX IF NOT EXISTS ix_games_disp ON games(disposals)",
        "CREATE INDEX IF NOT EXISTS ix_games_goals ON games(goals)",
        "CREATE INDEX IF NOT EXISTS ix_players_obsc ON players(obscurity)",
        "CREATE INDEX IF NOT EXISTS ix_final ON games(is_final, player_id, result)",
        "CREATE INDEX IF NOT EXISTS ix_pc ON games(player_id, club_now)",
        "CREATE INDEX IF NOT EXISTS ix_cs ON games(club_now, season)",
        "CREATE INDEX IF NOT EXISTS ix_pl ON games(player)",
        "CREATE INDEX IF NOT EXISTS ix_players_key ON players(name_key)",
        "CREATE INDEX IF NOT EXISTS ix_games_result ON games(result)",
        "CREATE INDEX IF NOT EXISTS ix_ts ON team_seasons(season, club_now)",
        "CREATE INDEX IF NOT EXISTS ix_sg ON season_goals(player_id, season)",
        "CREATE INDEX IF NOT EXISTS ix_games_venue ON games(venue)",
        "CREATE INDEX IF NOT EXISTS ix_games_match ON games(season, date, club_hist)",
    ]:
        cur.execute(stmt)
    con.commit()

    span = f"{out.season.min()}-{out.season.max()}"
    # Idempotent: afl/build_db.py must be safe to re-run over an existing file
    # (e.g. `--refresh` mid-season). pandas' to_sql(if_exists="replace")
    # handles the data tables; meta is created here so it needs its own drop.
    con.execute("DROP TABLE IF EXISTS meta")
    con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('source', ?)", (DATA_URL,))
    con.execute("INSERT INTO meta VALUES ('seasons', ?)", (span,))
    con.execute("INSERT INTO meta VALUES ('built', datetime('now'))")
    con.commit()
    con.close()

    print(f"\nDone. {len(out):,} games, {len(players):,} players, seasons {span}")
    print("Note: disposal/mark/tackle data only exists from 1965 onward.")
    print("      Pre-1965 rows have goals only.")

    # to_sql(if_exists="replace") drops and recreates `games`, which takes the
    # match_id column with it. Rather than leave that as a step someone has to
    # remember, finish the job here: match_ids are keyed on match_key, so
    # existing ids are reused and nothing holding one is invalidated.
    if not skip_matches:
        print()
        try:
            from . import derive_matches
        except ImportError:
            print("afl/derive_matches.py not found -- games.match_id not rebuilt. "
                  "Run it manually, or use --no-matches to silence this.")
        else:
            derive_matches.run(db_path)

    # to_sql(if_exists="replace") takes games.match_event with it too, and the
    # marquee squares vanish with it. The tags are derived from cached
    # Wikipedia pages, so a retag is offline and idempotent -- it re-applies
    # the same event names to the same matches. Only a first run, with no
    # cache, needs the network; that failure is reported and skipped rather
    # than allowed to fail a build whose games table is already correct.
    print()
    try:
        from . import scrape_marquee_games
    except ImportError:
        print("afl/scrape_marquee_games.py not found -- games.match_event "
              "not rebuilt.")
    else:
        con = sqlite3.connect(db_path)
        try:
            scrape_marquee_games.scrape_and_update(con)
        except Exception as error:                              # noqa: BLE001
            print(f"warning: marquee tagging skipped ({error})",
                  file=sys.stderr)
        finally:
            con.close()

    # The ladder built above orders on points and percentage alone, which is
    # ambiguous when both tie, and it does not carry the club_path_* columns.
    # repair_database.py is the canonical fix and is idempotent, so run it here
    # rather than leaving it as a step someone has to remember -- forgetting it
    # produces a database that looks complete but has the wrong wooden spoons.
    print()
    try:
        import repair_database
    except ImportError:
        print("repair_database.py not found -- ladder repair not applied.")
    else:
        repair_database.run(db_path)


#: The AFL obscurity model. The machinery is shared and lives in
#: obscurity.py; the terms and weights are the sport's and live in
#: afl/obscurity_model.py.
#:
#: These four names stay here because recompute_obscurity.py, the star
#: disclaimer and tests/test_obscurity.py all import them from build_db,
#: and there is no reason to make callers care where the code moved to.
AFL_MODEL = obscurity_model.MODEL
OBSCURITY_WEIGHTS = AFL_MODEL.weights

#: Bumped whenever the formula changes, and stored alongside every score so
#: a database can say which model produced it. Scores from different
#: versions are not comparable. The version history is on the MODEL itself.
OBSCURITY_MODEL_VERSION = AFL_MODEL.version

#: Terms that can be unavailable rather than zero. Everything else is
#: derived from the games rows and is therefore always known.
OBSCURITY_OPTIONAL_TERMS = AFL_MODEL.optional_terms


def obscurity_components(p):
    """Per-term contributions, the blended score and its confidence.

    The AFL binding of obscurity.components. The formula, its rationale and
    the missing-is-not-zero rule all live in obscurity.py.
    """
    return obscurity.components(p, AFL_MODEL)


def obscurity_score(p):
    """The blended 0-100 score alone. See `obscurity_components`."""
    return obscurity.score(p, AFL_MODEL)


# ---------------------------------------------------------------- layers

def refresh_layers(db_path, verbose=True):
    """Re-link every optional import layer against a freshly built database.

    A rebuild reassigns player_id values, so each layer that resolves names to
    ids has to be re-run afterwards or it silently points at the wrong people.

    Every step is individually tolerant: a layer whose source files are not
    present locally is skipped with a note rather than failing the build. What
    is NOT tolerated is a layer writing somewhere other than `db_path` -- that
    is how a rebuild previously ended up split across two database files, one
    holding the core stats and the other holding the enrichment.
    """
    import subprocess
    from data_paths import raw_dir

    def step(label, fn):
        print()
        print(f"-- {label}")
        try:
            fn()
        except Exception as exc:
            print(f"   {label} skipped: {exc}")

    def script(name, *args):
        """Run a loader that only exposes a command line, with an explicit --db."""
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            name), "--db", str(db_path), *args]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"{name} exited {result.returncode}")

    # Draft, awards and All-Australian come from the Draftguru scrape, then are
    # linked to player_ids in two further passes. Skipped when unscraped.
    def draftguru():
        root = raw_dir("afl") / "draftguru"
        if not root.is_dir():
            raise RuntimeError(f"no Draftguru scrape at {root}")
        script("afl/load_draftguru.py", "--root", str(root))
        script("afl/link_draft.py")
        script("afl/link_people.py")

    step("draft, awards and All-Australian", draftguru)

    def captains():
        from . import load_captains
        load_captains.refresh_default(db_path=str(db_path), verbose=verbose)

    step("club captaincy", captains)

    def rising_star():
        from . import load_rising_star
        load_rising_star.refresh_default(db_path=str(db_path), verbose=verbose)

    step("Rising Star nominations", rising_star)

    def brownlow():
        from . import load_brownlow
        load_brownlow.refresh_default(db_path=str(db_path), verbose=verbose)

    step("Brownlow voting results", brownlow)

    def family_draft():
        from . import load_family_draft
        load_family_draft.refresh_default(db_path=str(db_path), verbose=verbose)

    step("family draft", family_draft)

    def family_relationships():
        from . import load_family_relationships
        load_family_relationships.refresh_default(str(db_path), verbose=verbose)

    step("family relationships", family_relationships)

    def club_sources():
        from utils import load_club_sources
        load_club_sources.refresh_default(db_path=str(db_path), verbose=verbose)

    step("club metadata and records", club_sources)

    def club_all_games():
        from utils import load_club_all_games
        raw = load_club_all_games.DEFAULT_RAW_DIR
        if not raw.is_dir():
            raise RuntimeError(f"no cached club pages at {raw}")
        load_club_all_games.run(Path(db_path), raw)

    step("club all-games match sources", club_all_games)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db("afl"))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-matches", action="store_true",
                    help="skip the afl/derive_matches.py step")
    ap.add_argument("--core-only", action="store_true",
                    help="skip every optional import layer")
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="build even when duplicate player-game rows cannot "
                         "be resolved (career totals will be inflated)")
    a = ap.parse_args()
    build(a.db, a.refresh, a.no_matches, strict=not a.allow_duplicates)
    if not a.core_only:
        print()
        print(f"Refreshing optional layers -> {a.db}")
        refresh_layers(a.db)
