#!/usr/bin/env python3
"""
fetch_grid.py -- Pull a day's grid from gridleygame.com.

    python fetch_grid.py 2026-07-27
    python fetch_grid.py today
    python fetch_grid.py 2026-07-27 --solve      # fetch, parse, solve all 9
    python fetch_grid.py 2026-07-27 --discover   # dump what the site returns

IMPORTANT: gridleygame.com is a Next.js app that renders the grid in the
browser, so the criteria are not in the plain HTML. This script tries the
places Next.js usually stashes the data:

  1. the __NEXT_DATA__ JSON blob embedded in the page
  2. /_next/data/<buildId>/<date>.json  (buildId read from step 1)
  3. a few likely /api/ paths

This could not be verified against the live site when it was written, so if
all three miss, run --discover. That saves the raw HTML and any JSON found
to ./discover/ so you can see the real shape, and the parser can be pointed
at it in one line (see FIELD_HINTS below).

One request per day is a negligible load, but there is a delay between the
fallback attempts regardless.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request

BASE = "https://gridleygame.com"
UA = "sports-data-lab/1.1 (personal use; one request per day)"


def default_db():
    """Prefer the multi-sport data layout, with legacy compatibility."""
    from data_paths import sport_db
    return sport_db("afl")

# If --discover shows the criteria live under different key names, add them
# here rather than rewriting the walker.
FIELD_HINTS = {
    "rows": ["rows", "rowCriteria", "rowCategories", "yAxis", "vertical"],
    "cols": ["cols", "columns", "colCriteria", "colCategories", "xAxis",
             "horizontal"],
    "label": ["label", "name", "title", "text", "display", "description",
              "shortName", "club"],
}


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def next_data(html):
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def walk_for_grid(obj, depth=0):
    """Find a dict holding both a rows-ish and cols-ish key."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        keys = {k.lower(): k for k in obj}
        rk = next((keys[h.lower()] for h in FIELD_HINTS["rows"]
                   if h.lower() in keys), None)
        ck = next((keys[h.lower()] for h in FIELD_HINTS["cols"]
                   if h.lower() in keys), None)
        if rk and ck:
            r, c = obj[rk], obj[ck]
            if isinstance(r, list) and isinstance(c, list) \
                    and len(r) == 3 and len(c) == 3:
                return {"rows": r, "cols": c}
        for v in obj.values():
            found = walk_for_grid(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = walk_for_grid(v, depth + 1)
            if found:
                return found
    return None


def to_label(item):
    """Reduce a criterion object to its display string."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for h in FIELD_HINTS["label"]:
            for k in item:
                if k.lower() == h.lower() and isinstance(item[k], str):
                    return item[k]
        # A club square often nests the club under 'team'.
        for k in ("team", "club"):
            if k in item:
                return to_label(item[k])
    return str(item)


def fetch(date, discover=False):
    attempts = []

    html = None
    try:
        html = get(f"{BASE}/{date}")
        attempts.append(("page HTML", "ok"))
    except Exception as e:
        attempts.append(("page HTML", f"failed: {e}"))

    if discover:
        os.makedirs("discover", exist_ok=True)
        if html:
            with open(f"discover/{date}.html", "w", encoding="utf-8") as f:
                f.write(html)
            print(f"saved discover/{date}.html ({len(html):,} bytes)")

    nd = next_data(html) if html else None
    if nd:
        attempts.append(("__NEXT_DATA__", "found"))
        if discover:
            with open(f"discover/{date}.next.json", "w", encoding="utf-8") as f:
                json.dump(nd, f, indent=2)
            print(f"saved discover/{date}.next.json")
            print("top-level keys:", list(nd.keys()))
            props = nd.get("props", {}).get("pageProps", {})
            print("pageProps keys:", list(props.keys())
                  if isinstance(props, dict) else type(props))
        grid = walk_for_grid(nd)
        if grid:
            return grid, attempts

        # 2. the data route
        build = nd.get("buildId")
        if build:
            time.sleep(1)
            url = f"{BASE}/_next/data/{build}/{date}.json"
            try:
                blob = json.loads(get(url))
                attempts.append(("data route", "ok"))
                if discover:
                    with open(f"discover/{date}.route.json", "w",
                              encoding="utf-8") as f:
                        json.dump(blob, f, indent=2)
                    print(f"saved discover/{date}.route.json")
                grid = walk_for_grid(blob)
                if grid:
                    return grid, attempts
            except Exception as e:
                attempts.append(("data route", f"failed: {e}"))
    else:
        attempts.append(("__NEXT_DATA__", "not present"))

    # 3. plausible API paths
    for path in (f"/api/grid/{date}", f"/api/game/{date}",
                 f"/api/grids/{date}", f"/api/puzzle/{date}"):
        time.sleep(1)
        try:
            blob = json.loads(get(BASE + path))
            attempts.append((path, "ok"))
            if discover:
                with open(f"discover/{date}.api.json", "w",
                          encoding="utf-8") as f:
                    json.dump(blob, f, indent=2)
                print(f"saved discover/{date}.api.json")
            grid = walk_for_grid(blob)
            if grid:
                return grid, attempts
        except Exception as e:
            attempts.append((path, f"failed: {e}"))

    return None, attempts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYY-MM-DD, or 'today'")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--solve", action="store_true")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()

    date = (datetime.date.today().isoformat()
            if a.date == "today" else a.date)

    grid, attempts = fetch(date, a.discover)

    print(f"\nAttempts for {date}:")
    for what, how in attempts:
        print(f"  {what:<22} {how}")

    if not grid:
        print("\nCouldn't locate the grid data automatically.")
        print("Run with --discover, then look in ./discover/ for where the")
        print("criteria live and add those key names to FIELD_HINTS in this")
        print("file. Meanwhile you can type the six criteria into app.py.")
        sys.exit(1)

    rows = [to_label(x) for x in grid["rows"]]
    cols = [to_label(x) for x in grid["cols"]]
    print(f"\nRows:    {rows}")
    print(f"Columns: {cols}")

    import parse_criteria as P
    prows, pcols, problems = P.parse_grid(rows, cols)
    if problems:
        print("\nCouldn't interpret:")
        for p in problems:
            print(f"  - {p}")

    if a.solve:
        import sqlite3
        import constraints as C
        con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        if hasattr(C, "ensure_captain_table"):
            C.ensure_captain_table(con)
        for rlab, rcon in prows:
            for clab, ccon in pcols:
                print(f"\n=== {rlab}  x  {clab} ===")
                if not rcon or not ccon:
                    print("   (skipped, criterion not supported)")
                    continue
                got = C.solve(con, [rcon, ccon], limit=a.limit)
                if not got:
                    print("   no players satisfy both")
                for g in got:
                    print(f"   {g[0]:<24}{g[1]}-{g[2]}  "
                          f"{g[3]:>3}g  obsc {g[7]}")


if __name__ == "__main__":
    main()
