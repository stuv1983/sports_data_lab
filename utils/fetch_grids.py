#!/usr/bin/env python3
"""
scripts/fetch_grids.py -- Scrape grids and save them directly to the database.

Usage:
    python scripts/fetch_grids.py afl 2026-07-27
    python scripts/fetch_grids.py nba today
    python scripts/fetch_grids.py mlb 1104
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_paths import sport_db

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"

FIELD_HINTS = {
    "rows": ["rows", "rowCriteria", "rowCategories", "yAxis", "vertical"],
    "cols": ["cols", "columns", "colCriteria", "colCategories", "xAxis", "horizontal"],
    "label": ["label", "name", "title", "text", "display", "description", "shortName", "club"],
}

def get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def walk_for_grid(obj, depth=0):
    if depth > 12:
        return None
    if isinstance(obj, dict):
        keys = {k.lower(): k for k in obj}
        rk = next((keys[h.lower()] for h in FIELD_HINTS["rows"] if h.lower() in keys), None)
        ck = next((keys[h.lower()] for h in FIELD_HINTS["cols"] if h.lower() in keys), None)
        if rk and ck:
            r, c = obj[rk], obj[ck]
            if isinstance(r, list) and isinstance(c, list) and len(r) == 3 and len(c) == 3:
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
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for h in FIELD_HINTS["label"]:
            for k in item:
                if k.lower() == h.lower() and isinstance(item[k], str):
                    return item[k]
        for k in ("team", "club"):
            if k in item:
                return to_label(item[k])
    return str(item)


def gridley_label(item):
    """Return the complete criterion Gridley renders across two lines."""
    if not isinstance(item, dict):
        return to_label(item)
    title = str(item.get("title") or item.get("id") or "").strip()
    subtitle = str(item.get("subtitle") or "").strip()
    if not subtitle:
        return title
    if title.casefold() in subtitle.casefold():
        return subtitle
    return f"{title} {subtitle}".strip()

def fetch_gridley(date):
    BASE = "https://gridleygame.com"
    html = None
    try:
        html = get(f"{BASE}/data/grids/{date}.json")
    except Exception as e:
        print(f"Failed to fetch {BASE}/data/grids/{date}.json: {e}")
        return None

    try:
        nd = json.loads(html)
        # Gridley changed format: direct JSON with hItems and vItems
        if "hItems" in nd and "vItems" in nd:
            rows = [gridley_label(item) for item in nd["vItems"]]
            cols = [gridley_label(item) for item in nd["hItems"]]
            return {"rows": rows, "cols": cols}
            
        # fallback to original
        grid = walk_for_grid(nd)
        if grid: return grid
    except Exception as e:
        print(f"Failed to parse Gridley JSON: {e}")
        pass

    print("Could not find grid data for Gridley.")
    return None

def fetch_immaculate(sport, date_or_num):
    # Scaffold for Immaculate Grid (NBA/MLB) using Playwright to bypass Cloudflare
    print("Fetching Immaculate Grid using Playwright...")
    BASE = "https://www.sports-reference.com/immaculate-grid"
    sport_path = "basketball/mens" if sport == "nba" else "baseball"
    
    url = f"{BASE}/{sport_path}"
    if date_or_num != datetime.date.today().isoformat() and date_or_num != "today":
        url = f"{url}/grid-{date_or_num}"

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url)
            page.wait_for_timeout(5000)
            html = page.content()
            browser.close()
    except ImportError:
        print("Playwright not installed. Please run: pip install playwright && playwright install")
        return None
    except Exception as e:
        print(f"Error fetching immaculate grid with playwright: {e}")
        return None

    # Parse
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            nd = json.loads(m.group(1))
            grid = walk_for_grid(nd)
            if grid: return grid
        except:
            pass
    return None


def save_grid(db_path, date, source, rows, cols):
    """Insert or refresh one captured grid and return the action performed."""
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS historic_grids (
                grid_num INTEGER PRIMARY KEY,
                date TEXT,
                source TEXT,
                rows_json TEXT,
                cols_json TEXT,
                unsupported_json TEXT,
                note TEXT
            )
        """)

        existing = con.execute(
            "SELECT grid_num FROM historic_grids "
            "WHERE source=? AND date=? ORDER BY grid_num LIMIT 1",
            (source, date),
        ).fetchone()
        if existing:
            con.execute("""
                UPDATE historic_grids
                SET rows_json=?, cols_json=?, unsupported_json='[]', note=''
                WHERE grid_num=?
            """, (json.dumps(rows), json.dumps(cols), existing[0]))
            con.execute(
                "DELETE FROM historic_grids "
                "WHERE source=? AND date=? AND grid_num<>?",
                (source, date, existing[0]),
            )
            return "updated"

        con.execute("""
            INSERT INTO historic_grids (
                date, source, rows_json, cols_json, unsupported_json, note
            ) VALUES (?, ?, ?, ?, '[]', '')
        """, (date, source, json.dumps(rows), json.dumps(cols)))
        return "inserted"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=["afl", "nba", "mlb"])
    ap.add_argument("date_or_num", help="YYYY-MM-DD for AFL, or grid number for NBA/MLB, or 'today'")
    a = ap.parse_args()

    date = (datetime.date.today().isoformat() if a.date_or_num == "today" else a.date_or_num)

    if a.sport == "afl":
        grid = fetch_gridley(date)
        source = "Gridley"
    else:
        grid = fetch_immaculate(a.sport, date)
        source = "Immaculate Grid"

    if not grid:
        print("Failed to fetch grid.")
        sys.exit(1)

    rows = [to_label(x) for x in grid["rows"]]
    cols = [to_label(x) for x in grid["cols"]]

    db_path = sport_db(a.sport)
    
    action = save_grid(db_path, date, source, rows, cols)
    print(f"Successfully {action} grid for {date} in the {a.sport} database!")

if __name__ == "__main__":
    main()
