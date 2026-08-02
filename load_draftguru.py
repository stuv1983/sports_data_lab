#!/usr/bin/env python3
"""
load_draftguru.py -- Load a scraped Draftguru CSV tree into gridley.db.

Replaces fetch_draft.py: same destination `draft` table (so link_draft.py
keeps working unchanged), plus three new tables the scrape makes possible.

    python load_draftguru.py --root data/afl/raw/draftguru
    python load_draftguru.py --root data/afl/raw/draftguru --inspect   # headers only

Tables written
--------------
dg_people      one row per distinct Draftguru Player URL. The URL is a
               stable per-person key, so award rows and draft rows for the
               same person join without going through their name.
draft          one row per draft/trade/free-agency selection, 1981-2025.
               Column names match what fetch_draft.py produced.
awards         long/tidy: one row per (award, year, player). Every award
               schema in the scrape folds into this shape; columns that
               don't apply to a category are NULL.
all_australian one row per team selection, with position and captaincy.

Nothing here links a Draftguru person to an AFL Tables player_id. That is
link_people.py's job, and it is separate for the same reason link_draft.py
is: name alone is not a safe key.

The loader is deliberately loud about schema drift. If a source column it
expects is absent it says so per file rather than silently writing NULLs,
because a quietly-empty column looks identical to a genuinely blank one
once it is in the database.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys

from names import normalise_name

# ---------------------------------------------------------------- headers

# Draftguru's own headings, normalised (NBSP collapsed, "# ↧" -> "#"),
# mapped to database column names. Anything not listed is carried through
# only if it appears in KEEP_RAW.
HEADER_MAP = {
    # identity
    "player": "player", "player url": "player_url",
    "club": "club", "club url": "club_url",
    "original club": "original_club", "original club url": "original_club_url",
    "from": "original_club", "from url": "original_club_url",
    # draft-page fields
    # NOTE: Draftguru's "Pick" column is not the pick number. It holds notes
    # such as "Compensation ( Tom Scully )". The selection number lives under
    # the sortable "# \u21a7" heading. Verified against the 2012 National
    # Draft: Lachie Whitfield is #1, and every "Pick" cell there is either
    # blank or a compensation note.
    "pick": "pick_note", "pick url": "pick_note_url",
    "#": "pick",
    "draft": "draft_type_src", "signing": "signing",
    "signing url": "signing_url",
    "detail": "detail", "detail url": "detail_url",
    "age": "age_raw", "height": "height_raw", "weight": "weight_raw",
    "grade": "grade",
    # award-page fields
    "votes": "votes", "position": "position", "captain": "captain",
    "prior games": "prior_games", "games pre": "prior_games",
    "season games": "season_games", "season goals": "season_goals",
    "games": "games", "goals": "goals",
    "career games": "career_games", "clubs": "clubs_text",
    "drafted": "drafted_text", "times aa": "times_aa",
    "coaches": "coaches_votes", "coaches votes": "coaches_votes",
    "brownlow": "brownlow_votes", "brownlow votes": "brownlow_votes",
    "awards": "awards_text", "honours": "awards_text",
    "column 2": "note",
    # metadata the scraper added
    "year": "year", "year url": "year_url",
    "competition": "competition", "record category": "record_category",
    "draft type": "draft_type_meta",
    "award category": "award_category", "award name": "award_name",
    "award slug": "award_slug",
    "page title": "page_title", "source url": "source_url",
    "scraped at utc": "scraped_at", "source row": "source_row",
}


def norm_header(h):
    """'# ↧' -> '#', 'Original Club' -> 'original club'."""
    h = str(h).replace("\u00a0", " ").replace("\u200b", " ")
    h = h.replace("↧", " ").replace("↥", " ").replace("▼", " ")
    return re.sub(r"\s+", " ", h).strip().lower()


def clean_text(s):
    import pandas as pd
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).replace("\u00a0", " ").replace("\u200b", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def first_int(s):
    """'18yr 214d' -> 18, '188cm' -> 188, '' -> None."""
    s = clean_text(s)
    if s is None:
        return None
    m = re.search(r"-?\d+", s.replace(",", ""))
    return int(m.group()) if m else None


def read_csv(path):
    import pandas as pd
    # utf-8-sig: the scrape writes a BOM, which would otherwise ride along
    # on the first heading of every file.
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig",
                     keep_default_na=False, na_values=[""])
    df.columns = [norm_header(c) for c in df.columns]
    return df


def remap(df, path, expect=()):
    """Rename known headers; report unknown and missing ones."""
    out = {}
    unknown = []
    for col in df.columns:
        dest = HEADER_MAP.get(col)
        if dest is None:
            unknown.append(col)
            continue
        if dest not in out:                      # first wins on collisions
            out[dest] = df[col].map(clean_text)
    import pandas as pd
    res = pd.DataFrame(out)
    missing = [c for c in expect if c not in res.columns]
    return res, unknown, missing


# ------------------------------------------------------------------ files

def find(root, *parts):
    return os.path.join(root, *parts)


def collect(root, category):
    """Prefer a combined file; fall back to globbing the per-item folder."""
    combos = {
        "draft": [("afl_vfl_draft_years_1981_2025.csv",), ("draft_years", "*.csv")],
        "all_australian": [("all_australian_teams_1979_2025.csv",),
                           ("all_australian_by_year", "*.csv")],
        "club_bnf": [("club_best_and_fairests.csv",),
                     ("club_best_and_fairest", "*.csv")],
        "awards": [("awards", "*.csv")],
        "pick1": [("national_draft_pick_1.csv",)],
    }
    found = []
    for parts in combos[category]:
        pat = find(root, *parts)
        hits = sorted(glob.glob(pat))
        if hits:
            found = hits
            break
    return found


# ------------------------------------------------------------------ build

def build_draft(root, report):
    import pandas as pd
    files = collect(root, "draft")
    if not files:
        print("  no draft-year files found")
        return pd.DataFrame()

    frames = []
    for f in files:
        df = read_csv(f)
        res, unknown, missing = remap(df, f, expect=["player", "year"])
        report.append((os.path.relpath(f, root), len(df), unknown, missing))
        if missing:
            print(f"  SKIP {os.path.basename(f)}: missing {missing}")
            continue
        frames.append(res)
    if not frames:
        return pd.DataFrame()

    d = pd.concat(frames, ignore_index=True)
    d = d[d["player"].notna()].copy()

    # The 1981-style pages carry no Draft column, so the scraper's
    # metadata Draft Type is the fallback. Keep both provenances visible.
    d["draft_type"] = d.get("draft_type_src")
    if "draft_type_meta" in d:
        d["draft_type"] = d["draft_type"].fillna(d["draft_type_meta"])

    d["draft_year"] = d["year"].map(first_int)
    d["pick"] = d["pick"].map(first_int) if "pick" in d else None

    # Signing is where father-son, academy, zone and free-agency selections
    # are recorded, as "Father-Son ( Anthony Daniher )" or "Academy (GWS)".
    # Split the kind from the detail so a constraint can match the kind
    # exactly rather than by substring.
    if "signing" in d:
        d["signing_kind"] = (d["signing"].str.split("(").str[0]
                             .str.strip().replace("", None))
        d["signing_detail"] = (d["signing"].str.extract(r"\(([^)]*)\)")[0]
                               .str.strip().replace("", None))
    else:
        d["signing"] = d["signing_kind"] = d["signing_detail"] = None
    d["draft_age"] = d["age_raw"].map(first_int) if "age_raw" in d else None
    d["height_cm"] = d["height_raw"].map(first_int) if "height_raw" in d else None
    d["weight_kg"] = d["weight_raw"].map(first_int) if "weight_raw" in d else None
    for c in ("games", "goals", "coaches_votes", "brownlow_votes"):
        if c in d:
            d[c] = d[c].map(first_int)

    d["name_key"] = d["player"].map(normalise_name)

    import pandas as _pd
    for c in ("draft_year", "pick", "draft_age", "height_cm", "weight_kg",
              "games", "goals", "coaches_votes", "brownlow_votes"):
        if c in d:
            d[c] = _pd.array(d[c], dtype="Int64")

    cols = ["player", "player_url", "name_key", "draft_year", "draft_type",
            "pick", "pick_note", "club", "signing", "signing_kind",
            "signing_detail", "detail", "original_club",
            "draft_age", "height_cm", "weight_kg", "grade", "games", "goals",
            "coaches_votes", "brownlow_votes", "awards_text", "competition",
            "record_category", "source_url", "source_row"]
    for c in cols:
        if c not in d:
            d[c] = None
    return d[cols]


AWARD_EXPECT = ["player", "year"]


def build_awards(root, report):
    """Fold every award schema into one long table."""
    import pandas as pd
    frames = []
    for category, files in (("award", collect(root, "awards")),
                            ("club_best_and_fairest", collect(root, "club_bnf")),
                            ("national_draft_pick_1", collect(root, "pick1"))):
        for f in files:
            df = read_csv(f)
            res, unknown, missing = remap(df, f, expect=AWARD_EXPECT)
            report.append((os.path.relpath(f, root), len(df), unknown, missing))
            if missing:
                print(f"  SKIP {os.path.basename(f)}: missing {missing}")
                continue
            res["source_file"] = os.path.relpath(f, root)
            res["source_category"] = category
            if "award_slug" not in res:
                res["award_slug"] = os.path.splitext(os.path.basename(f))[0]
            frames.append(res)
    if not frames:
        return pd.DataFrame()

    a = pd.concat(frames, ignore_index=True)
    a = a[a["player"].notna()].copy()
    a["season"] = a["year"].map(first_int)
    a["name_key"] = a["player"].map(normalise_name)
    for c in ("votes", "prior_games", "season_games", "season_goals",
              "career_games", "games", "goals"):
        if c in a:
            a[c] = a[c].map(first_int)

    cols = ["award_category", "award_name", "award_slug", "source_category",
            "season", "player", "player_url", "name_key", "club",
            "original_club", "votes", "prior_games", "season_games",
            "season_goals", "career_games", "games", "goals", "drafted_text",
            "clubs_text", "note", "awards_text", "source_file", "source_url",
            "source_row"]
    for c in cols:
        if c not in a:
            a[c] = None
    return a[cols]


def build_all_australian(root, report):
    import pandas as pd
    files = collect(root, "all_australian")
    if not files:
        print("  no All-Australian team files found")
        return pd.DataFrame()
    frames = []
    for f in files:
        df = read_csv(f)
        res, unknown, missing = remap(df, f, expect=["player", "year"])
        report.append((os.path.relpath(f, root), len(df), unknown, missing))
        if missing:
            print(f"  SKIP {os.path.basename(f)}: missing {missing}")
            continue
        frames.append(res)
    if not frames:
        return pd.DataFrame()

    t = pd.concat(frames, ignore_index=True)
    t = t[t["player"].notna()].copy()
    t["season"] = t["year"].map(first_int)
    t["name_key"] = t["player"].map(normalise_name)
    # Captain is a marker column holding "C" or "VC", not a name. Treating
    # anything non-empty as captain would promote 21 vice-captains.
    cap = t["captain"] if "captain" in t else None
    t["is_captain"] = (cap.str.upper().eq("C").fillna(False).astype(int)
                       if cap is not None else 0)
    t["is_vice_captain"] = (cap.str.upper().eq("VC").fillna(False).astype(int)
                            if cap is not None else 0)
    for c in ("times_aa", "prior_games", "games"):
        if c in t:
            t[c] = t[c].map(first_int)
    cols = ["season", "player", "player_url", "name_key", "club", "position",
            "is_captain", "is_vice_captain", "times_aa", "prior_games", "games", "drafted_text",
            "source_url", "source_row"]
    for c in cols:
        if c not in t:
            t[c] = None
    return t[cols]


def build_people(frames):
    """One row per distinct Player URL, plus URL-less people keyed by name."""
    import pandas as pd
    parts = []
    for df in frames:
        if len(df):
            parts.append(df[["player", "player_url", "name_key"]])
    if not parts:
        return pd.DataFrame(columns=["dg_person_id", "player_url", "player",
                                     "name_key"])
    p = pd.concat(parts, ignore_index=True)
    # A person with a URL is identified by it. A person without one falls
    # back to their name key, flagged so link_people.py can treat those
    # rows with less confidence.
    p["person_key"] = p["player_url"].fillna("name:" + p["name_key"])
    p = (p.sort_values("player_url", na_position="last")
           .drop_duplicates(subset=["person_key"], keep="first")
           .reset_index(drop=True))
    p.insert(0, "dg_person_id", p.index + 1)
    p["has_url"] = p["player_url"].notna().astype(int)
    return p[["dg_person_id", "person_key", "player_url", "has_url",
              "player", "name_key"]]


def attach_person(df, people):
    import pandas as pd
    if not len(df):
        df["dg_person_id"] = None
        return df
    key = df["player_url"].fillna("name:" + df["name_key"])
    m = dict(zip(people["person_key"], people["dg_person_id"]))
    df = df.copy()
    df["dg_person_id"] = key.map(m).astype("Int64")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/afl/raw/draftguru")
    ap.add_argument("--db", default="data/afl/afl.db")
    ap.add_argument("--inspect", action="store_true",
                    help="report headers per file and write nothing")
    a = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        sys.exit("Run: pip install pandas")

    if not os.path.isdir(a.root):
        sys.exit(f"No such directory: {a.root}")

    report = []
    print("Draft years")
    draft = build_draft(a.root, report)
    print("Awards, club best-and-fairests, pick #1")
    awards = build_awards(a.root, report)
    print("All-Australian teams")
    aa = build_all_australian(a.root, report)

    people = build_people([draft, awards, aa])
    draft = attach_person(draft, people)
    awards = attach_person(awards, people)
    aa = attach_person(aa, people)

    unknown_all = sorted({u for _, _, un, _ in report for u in un})
    if unknown_all:
        print(f"\nHeadings seen but not mapped ({len(unknown_all)}): "
              f"{', '.join(unknown_all)}")
        print("Add them to HEADER_MAP if any of them matter.")

    if a.inspect:
        for rel, n, un, miss in report:
            print(f"  {rel:<48}{n:>6} rows"
                  + (f"  unmapped={un}" if un else "")
                  + (f"  MISSING={miss}" if miss else ""))
        return

    con = sqlite3.connect(a.db)
    people.to_sql("dg_people", con, if_exists="replace", index=False)
    draft.to_sql("draft", con, if_exists="replace", index=False)
    awards.to_sql("awards", con, if_exists="replace", index=False)
    aa.to_sql("all_australian", con, if_exists="replace", index=False)
    for stmt in [
        "CREATE INDEX IF NOT EXISTS ix_dg_people_key ON dg_people(name_key)",
        "CREATE INDEX IF NOT EXISTS ix_draft_key ON draft(name_key)",
        "CREATE INDEX IF NOT EXISTS ix_draft_person ON draft(dg_person_id)",
        "CREATE INDEX IF NOT EXISTS ix_awards_person ON awards(dg_person_id)",
        "CREATE INDEX IF NOT EXISTS ix_awards_slug ON awards(award_slug, season)",
        "CREATE INDEX IF NOT EXISTS ix_aa_person ON all_australian(dg_person_id)",
        "CREATE INDEX IF NOT EXISTS ix_aa_season ON all_australian(season)",
    ]:
        con.execute(stmt)
    con.commit()

    print(f"\ndg_people      {len(people):>7,}  "
          f"({int(people.has_url.sum()):,} with a Player URL)")
    print(f"draft          {len(draft):>7,}")
    print(f"awards         {len(awards):>7,}")
    print(f"all_australian {len(aa):>7,}")

    if len(draft):
        yrs = draft["draft_year"].dropna()
        print(f"draft years    {int(yrs.min())}-{int(yrs.max())}")
    if len(awards):
        n = awards["award_slug"].nunique()
        print(f"award pages    {n} distinct slugs")

    con.close()
    print("\nNext:  python link_draft.py  then  python link_people.py")
    print("Pick numbers restart per draft type, so a `pick` of 3 may be a")
    print("National, Rookie or Pre-Season selection. Constraints that mean")
    print("'top-10 pick' must filter draft_type as well.")


if __name__ == "__main__":
    main()
