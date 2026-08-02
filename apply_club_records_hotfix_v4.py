#!/usr/bin/env python3
"""Patch AFL Tables record headings that appear before their tables.

Place this file in the Sports Data Lab project root and run:
    python .\apply_club_records_hotfix_v4.py
"""
from __future__ import annotations
import argparse
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys

PATCH_MARKER = "Pair each record heading with the table that follows it."
PATCH_BLOCK = 'def _last_record_heading(text: str) -> str:\n    """Return the final AFL Tables record title in a text window."""\n    matches = list(re.finditer(\n        r"Most\\s+.+?\\s+In\\s+A\\s+(?:Season|Game)",\n        clean_text(text),\n        flags=re.I,\n    ))\n    return clean_text(matches[-1].group(0)) if matches else ""\n\n\ndef record_table_fragments(path: Path) -> list[tuple[str, str]]:\n    """Pair each record heading with the table that follows it.\n\n    The browser-saved AFL Tables page puts ``Most ... In A Season/Game`` in\n    the first table row. The live response fetched by ``urllib`` puts that\n    title immediately *before* the table. Supporting only the first form made\n    every downloaded records page validate but parse as zero rows.\n\n    Each return item is ``(heading, isolated_table_html)``. Isolating table\n    blocks keeps malformed legacy markup in one table from swallowing later\n    tables, while the preceding text window supports the live page layout.\n    """\n    data = path.read_bytes()\n    try:\n        raw = data.decode("utf-8")\n    except UnicodeDecodeError:\n        raw = data.decode("windows-1252", errors="replace")\n\n    blocks: list[tuple[str, str]] = []\n    table_pattern = re.compile(r"<table\\b[^>]*>.*?</table\\s*>", re.I | re.S)\n    for match in table_pattern.finditer(raw):\n        fragment = match.group(0)\n        table_text = clean_text(\n            BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)\n        )\n        heading = _last_record_heading(table_text)\n\n        # Live AFL Tables responses place the title before the table rather\n        # than inside it. Use the closest preceding title, not the previous\n        # table. A 12 KB window is comfortably larger than the title/link\n        # wrapper but smaller than a complete preceding record table.\n        if not heading:\n            prefix = raw[max(0, match.start() - 12000):match.start()]\n            prefix_text = BeautifulSoup(\n                prefix, "html.parser"\n            ).get_text(" ", strip=True)\n            heading = _last_record_heading(prefix_text)\n\n        if record_scope_and_stat(heading)[0]:\n            blocks.append((heading, fragment))\n\n    # Last-resort DOM walk for a page whose table closing tags are malformed.\n    # ``heading_before`` searches the preceding DOM nodes and therefore also\n    # handles the live title-before-table layout.\n    if not blocks:\n        soup = BeautifulSoup(raw, "html.parser")\n        for table in soup.find_all("table"):\n            table_text = clean_text(table.get_text(" ", strip=True))\n            heading = _last_record_heading(table_text) or heading_before(table)\n            if record_scope_and_stat(heading)[0]:\n                blocks.append((heading, str(table)))\n    return blocks\n\n\ndef parse_record_fragment(fragment: str, heading_hint: str = "") -> list[dict]:\n    """Parse one isolated paired-column record table."""\n    soup = BeautifulSoup(fragment, "html.parser")\n    rows = soup.find_all("tr")\n    if not rows:\n        return []\n\n    heading = ""\n    heading_index = -1\n    for index, row in enumerate(rows[:5]):\n        candidate = clean_text(row.get_text(" ", strip=True))\n        if record_scope_and_stat(candidate)[0]:\n            heading = _last_record_heading(candidate)\n            heading_index = index\n            break\n    if not heading:\n        heading = clean_text(heading_hint)\n    if not heading:\n        heading = _last_record_heading(\n            clean_text(soup.get_text(" ", strip=True))\n        )\n    scope, stat = record_scope_and_stat(heading)\n    if not scope:\n        return []\n\n    header_row_index = None\n    headers: list[str] = []\n    for index, row in enumerate(rows):\n        if index <= heading_index:\n            continue\n        cells = row_cells(row)\n        candidate = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]\n        if sum(value.casefold() == "player" for value in candidate) >= 1:\n            header_row_index = index\n            headers = candidate\n            break\n    if header_row_index is None or not headers:\n        return []\n\n    player_positions = [\n        index for index, value in enumerate(headers)\n        if value.casefold() == "player"\n    ]\n    if not player_positions:\n        return []\n\n    groups: list[list[tuple[dict[str, str], list[Any]]]] = [\n        [] for _ in player_positions\n    ]\n    for row in rows[header_row_index + 1:]:\n        cells = row_cells(row)\n        values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]\n        if len(values) != len(headers):\n            continue\n        for group_index, offset in enumerate(player_positions):\n            stop = (\n                player_positions[group_index + 1]\n                if group_index + 1 < len(player_positions)\n                else len(headers)\n            )\n            segment_headers = headers[offset:stop]\n            segment_values = values[offset:stop]\n            segment_cells = cells[offset:stop]\n            if segment_values and segment_values[0]:\n                groups[group_index].append((\n                    dict(zip(segment_headers, segment_values)),\n                    segment_cells,\n                ))\n\n    source_rows = [item for group in groups for item in group]\n    records: list[dict] = []\n    for rank, (raw, cells) in enumerate(source_rows, start=1):\n        player = first_present(raw, "Player")\n        if not player:\n            continue\n        player_cell = cells[0] if cells else None\n        match_description = first_present(raw, "Match")\n        season_value = first_present(raw, "Year", "Season")\n        opponent = first_present(raw, "Opponent", "Opp")\n        if match_description:\n            year_match = re.search(r"\\b(18|19|20)\\d{2}\\b", match_description)\n            if year_match and not season_value:\n                season_value = year_match.group(0)\n            opponent_match = re.search(r"\\bv\\s+(.+)$", match_description, re.I)\n            if opponent_match and not opponent:\n                opponent = clean_text(opponent_match.group(1))\n        value_text = first_present(raw, "#", "Total", "Value", "Stat", "Record")\n        records.append({\n            "scope": scope,\n            "stat": stat,\n            "source_heading": heading,\n            "source_rank": rank,\n            "player_name_source": player,\n            "player_name": source_name_to_display(player),\n            "player_url": player_href(player_cell),\n            "source_team": first_present(raw, "TM", "Team", "Tm"),\n            "value": parse_number(value_text),\n            "games": parse_number(first_present(raw, "GM", "Games"), integer=True),\n            "average": parse_number(first_present(raw, "Ave.", "Average", "Avg")),\n            "season": parse_number(season_value, integer=True),\n            "round": first_present(raw, "Round", "Rnd"),\n            "opponent": opponent,\n            "match_date": first_present(raw, "Date"),\n            "match_description": match_description,\n            "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),\n        })\n    return records\n\n\ndef parse_records(path: Path) -> list[dict]:\n    """Parse every AFL Tables season/game record table from raw HTML."""\n    fragments = record_table_fragments(path)\n    records = [\n        record\n        for heading, fragment in fragments\n        for record in parse_record_fragment(fragment, heading)\n    ]\n    if not records:\n        data = path.read_bytes()\n        try:\n            raw = data.decode("utf-8")\n        except UnicodeDecodeError:\n            raw = data.decode("windows-1252", errors="replace")\n        headings = len(re.findall(\n            r"Most\\s+.+?\\s+In\\s+A\\s+(?:Season|Game)", raw,\n            flags=re.I | re.S,\n        ))\n        tables = len(re.findall(r"<table\\b", raw, flags=re.I))\n        raise ValueError(\n            "no Season/Game Record tables parsed "\n            f"(source contains {headings} record headings and {tables} table tags)"\n        )\n    return records\n\n'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    target = root / "utils" / "load_club_sources.py"
    if not target.exists():
        print(f"error: missing {target}", file=sys.stderr)
        return 1

    current = target.read_text(encoding="utf-8")
    if PATCH_MARKER in current:
        print("unchanged utils\\load_club_sources.py")
    else:
        pattern = re.compile(
            r"def record_table_fragments\(path: Path\).*?(?=def parse_wikipedia)",
            re.S,
        )
        updated, count = pattern.subn(lambda _match: PATCH_BLOCK, current, count=1)
        if count != 1:
            print("error: parser patch anchor not found", file=sys.stderr)
            return 1
        backup = target.with_suffix(target.suffix + ".bak_club_records_v4")
        if not backup.exists():
            shutil.copy2(target, backup)
        target.write_text(updated, encoding="utf-8")
        print("patched  utils\\load_club_sources.py")

    print("\nCompiling parser...")
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(target)], check=False
    )
    if result.returncode:
        return result.returncode

    sys.path.insert(0, str(root / "utils"))
    spec = importlib.util.spec_from_file_location("club_loader_v4", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    pages = sorted((root / "data" / "afl" / "raw" / "clubs").glob(
        "*/afltables_records.html"
    ))
    if not pages:
        print("error: no cached club record pages found", file=sys.stderr)
        return 1

    print("Validating the actual cached club pages...")
    failures = []
    total = 0
    for page in pages:
        club = page.parent.name
        try:
            rows = module.parse_records(page)
            seasons = sum(row["scope"] == "season" for row in rows)
            games = sum(row["scope"] == "game" for row in rows)
            if not seasons or not games:
                raise ValueError(
                    f"parsed {len(rows)} rows but season={seasons}, game={games}"
                )
            total += len(rows)
            print(f"  {club:24} {len(rows):4} rows "
                  f"({seasons} season, {games} game)")
        except Exception as exc:
            failures.append(f"{club}: {type(exc).__name__}: {exc}")
    if failures:
        print("\nRecord validation failures:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"\nValidated {len(pages)} club pages and {total:,} record rows.")
    print("\nRebuild the club source layer with:")
    print("  python .\\utils\\load_club_sources.py --db .\\data\\afl\\afl.db --report --details")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
