#!/usr/bin/env python3
"""
build_db.py -- One-time (or occasional) build of the local Gridley database.

Pulls the community-maintained cached copy of the AFL Tables player-stats
dataset from GitHub (the same file the R package `fitzRoy` uses), normalises
it, and writes a local SQLite database.

This deliberately does NOT scrape afltables.com directly:
  * afltables.com/robots.txt disallows automated clients on the stats paths.
  * The fitzRoy_data mirror is a single ~14 MB file that is refreshed by its
    maintainers, so one download replaces tens of thousands of page requests.

Usage:
    python build_db.py                  # build ./gridley.db
    python build_db.py --refresh        # re-download even if cached
    python build_db.py --db my.db
    python build_db.py --no-matches     # skip the derive_matches.py step
"""

import argparse
import os
import sys
import sqlite3
import urllib.request
from pathlib import Path

from data_paths import default_db

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


def build(db_path, refresh=False, skip_matches=False):
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
    # derive_matches.py needs this to fill home_team/away_team properly.
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

    players["obscurity"] = obscurity_score(players)

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
    # Idempotent: build_db.py must be safe to re-run over an existing file
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
            import derive_matches
        except ImportError:
            print("derive_matches.py not found -- games.match_id not rebuilt. "
                  "Run it manually, or use --no-matches to silence this.")
        else:
            derive_matches.run(db_path)

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


#: Obscurity weights, and the whole of the tuning surface. They are
#: judgement, not a fit: the only ground truth available offline is a
#: handful of rarity percentages read off finished puzzles, which is far
#: too few to fit six coefficients against without inventing precision.
#: Career games stays the single strongest fame proxy; career span is the
#: term that separates "a whole career inside one season" from the same
#: game count spread over a decade.
OBSCURITY_WEIGHTS = {
    "games": 0.30,
    "span": 0.18,
    "era": 0.15,
    "goals": 0.14,
    "finals": 0.13,
    "brownlow": 0.10,
}


def obscurity_score(p):
    """
    Heuristic 0-100 proxy for how unlikely a player is to be picked.
    Higher = more obscure = lower expected Gridley rarity percentage.

    This is NOT Gridley's real pick data (which is crowd-sourced and not
    public). It is a fame proxy built from career footprint.

    TIES TAKE THE GROUP'S BEST RANK
    -------------------------------
    `method="min"` is the whole reason this scale reaches 100. With pandas'
    default `method="average"`, every member of a tied group takes the
    group's *midpoint* rank -- and these inputs are mostly ties: 82% of
    players never polled a Brownlow vote, 65% never played a final, 26%
    never kicked a goal. Averaging meant "never polled a vote" scored 58.8
    out of 100 rather than 100, so the most anonymous career possible
    topped out at 84.9 and the top sixth of the scale was unreachable.
    Having none of a thing puts a player in the most anonymous tier for
    that term; how many others share the tier is not evidence about them.

    CAREER SPAN
    -----------
    Seasons between debut and final game, which nothing else here captures.
    17 games all inside 1899 is a far more obscure career than 17 games
    strung across a decade, and only this term can tell them apart.
    """
    import numpy as np

    def pct_rank_low_is_obscure(s):
        # Invert so small values score high, and give a tied group its best
        # rank rather than the middle of the tie. See the docstring.
        return (1 - s.rank(pct=True, method="min")) * 100

    span = (p["final_season"].fillna(p["debut_season"])
            - p["debut_season"] + 1).clip(lower=1)

    terms = {
        "games": pct_rank_low_is_obscure(p["career_games"].fillna(0)),
        "span": pct_rank_low_is_obscure(span),
        "goals": pct_rank_low_is_obscure(p["career_goals"].fillna(0)),
        "brownlow": pct_rank_low_is_obscure(p["career_brownlow"].fillna(0)),
        "finals": pct_rank_low_is_obscure(p["finals_played"].fillna(0)),
        # Recency: modern players are far more familiar to today's solvers.
        # Peak obscurity sits in the pre-1990 era.
        "era": np.clip((2000 - p["final_season"]) / 80 * 100, 0, 100),
    }

    score = sum(OBSCURITY_WEIGHTS[name] * value for name, value in terms.items())
    return score.round(1)

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
        script("load_draftguru.py", "--root", str(root))
        script("link_draft.py")
        script("link_people.py")

    step("draft, awards and All-Australian", draftguru)

    def captains():
        import load_captains
        load_captains.refresh_default(db_path=str(db_path), verbose=verbose)

    step("club captaincy", captains)

    def rising_star():
        import load_rising_star
        load_rising_star.refresh_default(db_path=str(db_path), verbose=verbose)

    step("Rising Star nominations", rising_star)

    def family_draft():
        import load_family_draft
        load_family_draft.refresh_default(db_path=str(db_path), verbose=verbose)

    step("family draft", family_draft)

    def family_relationships():
        import load_family_relationships
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
                    help="skip the derive_matches.py step")
    ap.add_argument("--core-only", action="store_true",
                    help="skip every optional import layer")
    a = ap.parse_args()
    build(a.db, a.refresh, a.no_matches)
    if not a.core_only:
        print()
        print(f"Refreshing optional layers -> {a.db}")
        refresh_layers(a.db)
