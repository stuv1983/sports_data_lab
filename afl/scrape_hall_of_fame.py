#!/usr/bin/env python3
"""Fetch and parse the Australian Football Hall of Fame from Wikipedia.

    python -m afl.scrape_hall_of_fame            # fetch (cached), write CSV
    python -m afl.scrape_hall_of_fame --refresh  # re-fetch even if cached
    python -m afl.scrape_hall_of_fame --report   # parse counts, write nothing

Writes ``data/afl/raw/wikipedia_hall_of_fame.csv``; load it with
``utils/afl/load_hall_of_fame.py``.

Two requests, both to the MediaWiki API rather than to article HTML, so the
output is a documented interface rather than whatever the skin renders
today:

  1. ``action=parse`` on `Australian Football Hall of Fame`, whose tables
     carry the induction year and Legend status.
  2. ``action=query&list=categormembers`` on the inductees category, used
     only as a cross-check on the parsed membership. A name in the category
     but in no table is reported, not silently added -- the tables are the
     structured source and the category is a list of article titles.

The article's tables use rowspans heavily: a player who played for three
clubs occupies three rows sharing one induction year. `pandas.read_html`
expands those correctly, which is why the HTML is parsed rather than the
wikitext, where the same structure has to be tracked by hand.

Inductees are not all players. Coaches, umpires, administrators, media
figures and pioneers are inducted too, and most have no playing record in
this database at all. The category column keeps that distinction, so a
later "not linked to a player" count reads as expected rather than as a
failure.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
import urllib.request

from data_paths import cache_dir, raw_dir

API = "https://en.wikipedia.org/w/api.php"
ARTICLE = "Australian Football Hall of Fame"
CATEGORY = "Category:Australian Football Hall of Fame inductees"
USER_AGENT = "SportsDataLab/1.0 (personal research; contact via repository)"
OUTPUT = "wikipedia_hall_of_fame.csv"

FIELDS = ["name", "category", "inducted_year", "is_legend", "legend_year",
          "club", "state", "playing_career", "games_goals", "removed_year",
          "source_url"]

#: Table index -> (category, name column). Resolved by column signature
#: rather than position, because a new section would silently shift every
#: index after it.
SIGNATURES = [
    ("legend", {"Inductee", "Year elevated"}),
    ("player", {"Player", "Club", "Year inducted"}),
    ("coach", {"Coach", "Year inducted"}),
    ("umpire", {"Umpire", "Year Inducted"}),
    ("removed", {"Player", "Year removed"}),
]


def _flatten(columns) -> list[str]:
    """Collapse a MultiIndex header to its most specific non-repeated part."""
    out = []
    for col in columns:
        if isinstance(col, tuple):
            parts = [str(p) for p in col if not str(p).startswith("Unnamed")]
            # ('Legends','Inductee','Inductee') -> 'Inductee'
            seen, uniq = set(), []
            for part in parts:
                if part not in seen:
                    seen.add(part)
                    uniq.append(part)
            out.append(uniq[-1] if uniq else "")
        else:
            out.append(str(col))
    return out


def fetch(refresh: bool = False) -> tuple[str, list[str]]:
    """Article HTML and category membership, cached on disk."""
    folder = cache_dir("afl", "hall_of_fame")
    folder.mkdir(parents=True, exist_ok=True)
    html_path = folder / "article.html"
    members_path = folder / "category.json"

    def get(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    if refresh or not html_path.exists():
        url = (f"{API}?action=parse&page={urllib.parse.quote(ARTICLE)}"
               f"&prop=text&format=json&formatversion=2")
        payload = json.loads(get(url))
        html_path.write_text(payload["parse"]["text"], encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")

    if refresh or not members_path.exists():
        members, cont = [], None
        while True:
            url = (f"{API}?action=query&list=categorymembers"
                   f"&cmtitle={urllib.parse.quote(CATEGORY)}"
                   f"&cmlimit=500&cmtype=page&format=json&formatversion=2")
            if cont:
                url += f"&cmcontinue={urllib.parse.quote(cont)}"
            payload = json.loads(get(url))
            members.extend(m["title"] for m in
                           payload["query"]["categorymembers"])
            cont = payload.get("continue", {}).get("cmcontinue")
            if not cont:
                break
        members_path.write_text(json.dumps(members), encoding="utf-8")
    members = json.loads(members_path.read_text(encoding="utf-8"))
    return html, members


def _clean(value) -> str:
    """Strip footnote markers and normalise dashes in a cell.

    An empty pandas cell arrives as the float nan, whose str() is the word
    'nan' -- which then reads as real content everywhere downstream.
    """
    if value is None or value != value:          # NaN is not equal to itself
        return ""
    text = re.sub(r"\[\s*[a-z0-9]{1,4}\s*\]", "", str(value), flags=re.I)
    if text.strip().lower() in ("nan", "none", "-", "—", "–"):
        return ""
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _year(value) -> int | None:
    match = re.search(r"\b(18|19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


#: Sections rendered as bulleted lists rather than tables. These are the
#: non-playing categories, and between them they hold 39 inductees that a
#: tables-only parser misses entirely.
LIST_SECTIONS = {"Media": "media", "Administrators": "administrator",
                 "Pioneers": "pioneer"}


def parse_lists(html: str, source: str) -> list[dict]:
    """Inductees from the prose-list sections.

    Each entry is a list item whose first link is the person and whose
    parenthetical is their role, club or induction year depending on the
    section -- 'Norman Banks (radio, Victoria)' but 'John Acraman (2017)'.
    Only the Pioneers list carries a year, so the others are recorded
    without one rather than having a year invented for them.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for heading in soup.find_all(["h2", "h3"]):
        label = heading.get_text(" ", strip=True).split("[")[0].strip()
        category = LIST_SECTIONS.get(label)
        if category is None:
            continue
        node = heading.parent if heading.parent.name == "div" else heading
        for _ in range(6):
            node = node.find_next_sibling()
            if node is None or node.name in ("h2", "h3"):
                break
            items = node.find_all("li") if node.name in ("div", "ul") else []
            if not items:
                continue
            for item in items:
                link = item.find("a")
                name = _clean(link.get_text() if link else
                              item.get_text().split("(")[0])
                if not name:
                    continue
                detail = _clean(item.get_text())
                inside = re.search(r"\(([^)]*)\)", detail)
                note = inside.group(1) if inside else ""
                out.append({
                    "name": name, "category": category,
                    "inducted_year": _year(note) if category == "pioneer"
                                     else None,
                    "is_legend": 0, "legend_year": None,
                    "club": "" if category == "pioneer" else note,
                    "state": "", "playing_career": "", "games_goals": "",
                    "removed_year": None, "source_url": source,
                })
            break
    return out


def parse(html: str) -> list[dict]:
    """One row per inductee per club, merged to one row per person."""
    import pandas as pd

    tables = pd.read_html(io.StringIO(html))
    people: dict[str, dict] = {}
    legend_years: dict[str, int] = {}
    removed: dict[str, int] = {}
    source = ("https://en.wikipedia.org/wiki/"
              + urllib.parse.quote(ARTICLE.replace(" ", "_")))

    for table in tables:
        table = table.copy()
        table.columns = _flatten(table.columns)
        cols = set(table.columns)
        category = next((c for c, want in SIGNATURES if want <= cols), None)
        if category is None:
            continue
        # 'removed' shares its columns with 'player' plus one; check it first.
        if {"Year removed"} <= cols:
            category = "removed"

        name_col = next(c for c in ("Inductee", "Player", "Coach", "Umpire")
                        if c in cols)
        for _, row in table.iterrows():
            name = _clean(row.get(name_col))
            if not name or name.lower().startswith("nan"):
                continue

            if category == "legend":
                legend_years[name] = _year(row.get("Year elevated"))
                continue
            if category == "removed":
                removed[name] = _year(row.get("Year removed"))

            entry = people.setdefault(name, {
                "name": name, "category": category, "inducted_year": None,
                "is_legend": 0, "legend_year": None, "club": "",
                "state": "", "playing_career": "", "games_goals": "",
                "removed_year": None, "source_url": source,
            })
            entry["inducted_year"] = (entry["inducted_year"]
                                      or _year(row.get("Year inducted")))
            entry["state"] = entry["state"] or _clean(row.get("State"))
            # Rowspan expansion gives one row per club; keep them all.
            club = _clean(row.get("Club") or row.get("Clubs coached"))
            if club and club not in entry["club"].split(" | "):
                entry["club"] = f"{entry['club']} | {club}".strip(" |")
            career = _clean(row.get("Playing career"))
            if career and career not in entry["playing_career"]:
                entry["playing_career"] = (
                    f"{entry['playing_career']}, {career}".strip(", "))
            games = _clean(row.get("Games played (goals)"))
            if games and games not in entry["games_goals"]:
                entry["games_goals"] = (
                    f"{entry['games_goals']}, {games}".strip(", "))

    for name, year in legend_years.items():
        entry = people.setdefault(name, {
            "name": name, "category": "legend", "inducted_year": None,
            "is_legend": 0, "legend_year": None, "club": "", "state": "",
            "playing_career": "", "games_goals": "", "removed_year": None,
            "source_url": source,
        })
        entry["is_legend"] = 1
        entry["legend_year"] = year

    for entry in parse_lists(html, source):
        people.setdefault(entry["name"], entry)

    for name, year in removed.items():
        if name in people:
            people[name]["removed_year"] = year

    return sorted(people.values(), key=lambda r: r["name"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch even when a cached copy exists")
    ap.add_argument("--report", action="store_true",
                    help="parse and summarise, write no CSV")
    args = ap.parse_args(argv)

    html, members = fetch(refresh=args.refresh)
    rows = parse(html)

    by_category: dict[str, int] = {}
    for row in rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
    legends = sum(r["is_legend"] for r in rows)
    print(f"parsed {len(rows)} inductees  ({legends} legends)")
    for category, n in sorted(by_category.items(), key=lambda kv: -kv[1]):
        print(f"   {category:<10} {n:>4}")

    # The category is a cross-check, never a source of new rows: a title
    # there with no table row is a section this parser is not reading.
    parsed = {r["name"] for r in rows}
    titles = {re.sub(r"\s*\(.*\)$", "", t) for t in members
              if not t.startswith("Category:") and t != ARTICLE}
    only_category = sorted(titles - parsed)
    print(f"\ncategory members: {len(titles)}  "
          f"in category but not parsed: {len(only_category)}")
    for name in only_category[:15]:
        print(f"   {name}")
    if len(only_category) > 15:
        print(f"   ... and {len(only_category) - 15} more")
    if only_category:
        print("   These are mostly article titles that differ from the name "
              "the article's own tables use -- 'Chas Brownlow' for Charles "
              "Brownlow, 'Polly Farmer' for Graham Farmer. The tables are "
              "authoritative here; the category is only a completeness "
              "check, so a difference is reported rather than added.")

    if args.report:
        return 0

    folder = raw_dir("afl")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / OUTPUT
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {out}  ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
