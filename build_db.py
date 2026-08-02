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


def obscurity_score(p):
    """
    Heuristic 0-100 proxy for how unlikely a player is to be picked.
    Higher = more obscure = lower expected Gridley rarity percentage.

    This is NOT Gridley's real pick data (which is crowd-sourced and not
    public). It is a fame proxy built from career footprint.
    """
    import numpy as np

    def pct_rank_low_is_obscure(s):
        # invert: small values -> high score
        return (1 - s.rank(pct=True)) * 100

    games = pct_rank_low_is_obscure(p["career_games"].fillna(0))
    goals = pct_rank_low_is_obscure(p["career_goals"].fillna(0))
    brown = pct_rank_low_is_obscure(p["career_brownlow"].fillna(0))
    finals = pct_rank_low_is_obscure(p["finals_played"].fillna(0))

    # Recency: modern players are far more familiar to today's solvers.
    # Peak obscurity sits in the pre-1990 era.
    era = np.clip((2000 - p["final_season"]) / 80 * 100, 0, 100)

    score = (0.40 * games + 0.20 * brown + 0.15 * goals
             + 0.10 * finals + 0.15 * era)
    return score.round(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="gridley.db")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-matches", action="store_true",
                    help="skip the derive_matches.py step")
    a = ap.parse_args()
    build(a.db, a.refresh, a.no_matches)


# SDL_CAPTAIN_REFRESH — keep the optional club-captaincy layer in sync.
# Appended by apply_update.py. A rebuild can reassign player_id values, so
# the captaincy CSVs are re-linked after every successful build. Safe no-op
# when no CSVs exist under data/afl/raw/; never fails the build itself.
if __name__ == "__main__":
    try:
        import load_captains as _sdl_captains
        _sdl_captains.refresh_default(verbose=True)
    except Exception as _sdl_exc:
        print(f"captaincy refresh skipped: {_sdl_exc}")

# SDL_RISING_STAR_REFRESH — re-link the optional nomination layer after a
# clean database rebuild.  This is a safe no-op when no local CSV exists.
if __name__ == "__main__":
    try:
        import load_rising_star as _sdl_rising_star
        _sdl_rising_star.refresh_default(verbose=True)
    except Exception as _sdl_exc:
        print(f"Rising Star refresh skipped: {_sdl_exc}")


# SDL_FAMILY_DRAFT_REFRESH — re-link the local Wikipedia relationship layer
# after a clean database rebuild. Safe no-op when the CSV is not present.
if __name__ == "__main__":
    try:
        import load_family_draft as _sdl_family_draft
        _sdl_family_draft.refresh_default(verbose=True)
    except Exception as _sdl_exc:
        print(f"Family-draft refresh skipped: {_sdl_exc}")
