#!/usr/bin/env python3
"""Fetch and parse the Australian football Teams of the Century.

    python -m afl.scrape_teams_of_the_century            # fetch (cached), CSV
    python -m afl.scrape_teams_of_the_century --refresh  # re-fetch
    python -m afl.scrape_teams_of_the_century --report   # parse only

Writes ``data/afl/raw/wikipedia_teams_of_the_century.csv``; load it with
``afl/load_teams_of_the_century.py``.

Five teams, from four Wikipedia pages. Two of them are sections of a larger
article rather than pages of their own -- 'AFL Team of the Century' is a
redirect into the Australian Football League article, and the Queensland
team lives inside AFL Queensland -- so each source names its section
explicitly instead of relying on a title that resolves today.

All five render as the same thing: a positional grid, one row per line of
the ground, with the row label in the first cell::

    B :    Bernie Smith (Geelong, West Adelaide) | Stephen Silvagni ...
    HB :   Bruce Doull (Carlton) | Ted Whitten (Footscray) Captain | ...

So the parser reads a table row's label as the position and each remaining
cell as one selection. Clubs are in parentheses where the source gives
them; the Italian, Greek and Queensland teams mostly do not, and an absent
club is left empty rather than guessed at.

Captaincy is marked inconsistently -- 'Captain', '(c)', '(vc)' -- and is
normalised into its own column so a name never carries it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.parse
import urllib.request

from data_paths import cache_dir, raw_dir

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "SportsDataLab/1.0 (personal research; contact via repository)"
OUTPUT = "wikipedia_teams_of_the_century.csv"
FIELDS = ["team_name", "position", "sort_order", "name", "club", "role",
          "note", "source_url"]

#: team name -> (article, section index or None)
#:
#: A section index is a position in the article's own section list and can
#: move if the article is restructured. `--report` prints the row count per
#: team, so a source that has drifted shows up as a team that suddenly
#: parses zero selections rather than as silently missing data.
SOURCES = {
    "AFL/VFL Team of the Century": ("Australian Football League", 29),
    "Indigenous Team of the Century": ("Indigenous Team of the Century", None),
    "Italian Team of the Century": ("Italian Team of the Century", None),
    "Greek Team of the Century": ("Greek Team of the Century", None),
    "Queensland Team of the 20th Century": ("AFL Queensland", 6),
}

#: Row labels, normalised. The order is the order they are printed in, so
#: a team reads down the ground the way it is selected.
POSITIONS = {
    "b": "Back", "backs": "Back",
    "hb": "Half back", "half backs": "Half back",
    "c": "Centre", "centres": "Centre", "centre": "Centre",
    "hf": "Half forward", "half forwards": "Half forward",
    "f": "Forward", "forwards": "Forward",
    "foll": "Follower", "followers": "Follower", "ruck": "Follower",
    "int": "Interchange", "interchange": "Interchange", "bench": "Interchange",
    "res": "Reserve", "reserves": "Reserve",
    "coach": "Coach",
}
ORDER = ["Back", "Half back", "Centre", "Half forward", "Forward",
         "Follower", "Interchange", "Reserve", "Coach"]


def fetch(page: str, section: int | None, refresh: bool = False) -> str:
    folder = cache_dir("afl", "teams_of_the_century")
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", page.lower()).strip("_")
    path = folder / f"{slug}{'' if section is None else f'_s{section}'}.html"
    if refresh or not path.exists():
        url = (f"{API}?action=parse&page={urllib.parse.quote(page)}"
               f"{'' if section is None else f'&section={section}'}"
               f"&prop=text&format=json&formatversion=2")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        path.write_text(payload["parse"]["text"], encoding="utf-8")
    return path.read_text(encoding="utf-8")


def _label(text: str) -> str | None:
    key = re.sub(r"[^a-z ]", "", str(text or "").lower()).strip()
    return POSITIONS.get(key)


_ROLE = re.compile(r"\((c|vc|captain|vice[- ]captain)\)|(captain|vice[- ]captain)",
                   re.I)


def _selection(cell_text: str) -> tuple[str, str, str] | None:
    """(name, club, role) from one grid cell."""
    text = re.sub(r"\[\s*[a-z0-9]{1,4}\s*\]", "", cell_text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    role = ""
    match = _ROLE.search(text)
    if match:
        found = (match.group(1) or match.group(2) or "").lower()
        role = "Vice-captain" if found in ("vc", "vice-captain",
                                           "vice captain") else "Captain"
        text = _ROLE.sub("", text).strip()

    club = ""
    paren = re.search(r"\(([^)]*)\)\s*$", text)
    if paren:
        club = re.sub(r"\s*,\s*", ", ", paren.group(1)).strip()
        text = text[:paren.start()].strip()

    name = text.strip(" ,")
    return (name, club, role) if name else None


def parse(html: str, team: str, source_url: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for table in soup.find_all("table"):
        cells = table.find_all("tr")
        if not cells:
            continue
        # A positional grid is identified by its first column, not by a
        # class: these tables carry no consistent class across articles.
        labels = [_label(tr.find(["th", "td"]).get_text(" ", strip=True))
                  for tr in cells if tr.find(["th", "td"])]
        if sum(1 for x in labels if x) < 3:
            continue

        current = None
        for tr in cells:
            parts = tr.find_all(["th", "td"])
            if not parts:
                continue
            head = _label(parts[0].get_text(" ", strip=True))
            if head:
                current = head
                body = parts[1:]
            else:
                # A continuation row: the interchange bench often spills
                # onto a second, unlabelled line.
                body = parts if current else []
            for cell in body:
                found = _selection(cell.get_text(" ", strip=True))
                if not found:
                    continue
                name, club, role = found
                rows.append({
                    "team_name": team, "position": current or "",
                    "sort_order": ORDER.index(current) if current in ORDER
                                  else len(ORDER),
                    "name": name, "club": club, "role": role,
                    "note": "", "source_url": source_url,
                })
        if rows:
            break
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    all_rows: list[dict] = []
    for team, (page, section) in SOURCES.items():
        url = ("https://en.wikipedia.org/wiki/"
               + urllib.parse.quote(page.replace(" ", "_")))
        html = fetch(page, section, refresh=args.refresh)
        rows = parse(html, team, url)
        all_rows.extend(rows)
        captains = sum(1 for r in rows if r["role"] == "Captain")
        clubs = sum(1 for r in rows if r["club"])
        print(f"{team:<38} {len(rows):>3} selections  "
              f"({captains} captain, {clubs} with a club)")
        if not rows:
            print(f"   NOTHING PARSED — check {page}"
                  f"{'' if section is None else f' section {section}'}; "
                  "the article may have been restructured.")

    if args.report:
        return 0

    folder = raw_dir("afl")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / OUTPUT
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nwrote {out}  ({len(all_rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
