#!/usr/bin/env python3
"""Patch and validate the AFL Tables club records parser.

Place this file in the Sports Data Lab project root and run:

    python .\apply_club_records_hotfix_v5.py

The patch is idempotent, backs up ``utils/load_club_sources.py`` once, compiles
it, and validates all 18 cached ``afltables_records.html`` pages before keeping
the change.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys


NEW_FUNCTION = r'''def parse_record_fragment(fragment: str, heading_hint: str = "") -> list[dict]:
    """Parse one isolated paired-column record table.

    AFL Tables omits several closing ``</td>`` tags in the raw response. The
    standard BeautifulSoup ``html.parser`` therefore nests later cells inside
    the first cell. Parse row/cell boundaries from start tags instead: every
    new ``<td>`` or ``<th>`` starts the next cell, whether or not the previous
    cell was explicitly closed.
    """

    def fragment_text(markup: str) -> str:
        return clean_text(
            BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
        )

    def fragment_cells(row_markup: str) -> list[dict[str, str | None]]:
        pattern = re.compile(
            r"<(?P<tag>th|td)\b(?P<attrs>[^>]*)>"
            r"(?P<body>.*?)(?=<(?:th|td)\b|</tr\s*>|$)",
            flags=re.I | re.S,
        )
        cells: list[dict[str, str | None]] = []
        for match in pattern.finditer(row_markup):
            body = match.group("body")
            href = re.search(
                r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)",
                body,
                flags=re.I,
            )
            cells.append({
                "text": fragment_text(body),
                "href": href.group(1) if href else None,
            })
        return cells

    row_fragments = re.findall(
        r"<tr\b[^>]*>(.*?)</tr\s*>", fragment, flags=re.I | re.S
    )
    if not row_fragments:
        return []

    heading = clean_text(heading_hint)
    if not record_scope_and_stat(heading)[0]:
        for row_fragment in row_fragments[:5]:
            candidate = fragment_text(row_fragment)
            if record_scope_and_stat(candidate)[0]:
                heading = _last_record_heading(candidate)
                break
    scope, stat = record_scope_and_stat(heading)
    if not scope:
        return []

    header_row_index = None
    headers: list[str] = []
    for index, row_fragment in enumerate(row_fragments):
        cells = fragment_cells(row_fragment)
        candidate = [str(cell["text"] or "") for cell in cells]
        if any(value.casefold() == "player" for value in candidate):
            header_row_index = index
            headers = candidate
            break
    if header_row_index is None or not headers:
        return []

    player_positions = [
        index for index, value in enumerate(headers)
        if value.casefold() == "player"
    ]
    if not player_positions:
        return []

    groups: list[list[tuple[dict[str, str], list[dict[str, str | None]]]]] = [
        [] for _ in player_positions
    ]
    for row_fragment in row_fragments[header_row_index + 1:]:
        cells = fragment_cells(row_fragment)
        values = [str(cell["text"] or "") for cell in cells]
        if len(values) != len(headers):
            continue
        for group_index, offset in enumerate(player_positions):
            stop = (
                player_positions[group_index + 1]
                if group_index + 1 < len(player_positions)
                else len(headers)
            )
            segment_headers = headers[offset:stop]
            segment_values = values[offset:stop]
            segment_cells = cells[offset:stop]
            if segment_values and segment_values[0]:
                groups[group_index].append((
                    dict(zip(segment_headers, segment_values)),
                    segment_cells,
                ))

    source_rows = [item for group in groups for item in group]
    records: list[dict] = []
    for rank, (raw, cells) in enumerate(source_rows, start=1):
        player = first_present(raw, "Player")
        if not player:
            continue
        match_description = first_present(raw, "Match")
        season_value = first_present(raw, "Year", "Season")
        opponent = first_present(raw, "Opponent", "Opp")
        if match_description:
            year_match = re.search(r"\b(18|19|20)\d{2}\b", match_description)
            if year_match and not season_value:
                season_value = year_match.group(0)
            opponent_match = re.search(r"\bv\s+(.+)$", match_description, re.I)
            if opponent_match and not opponent:
                opponent = clean_text(opponent_match.group(1))
        value_text = first_present(raw, "#", "Total", "Value", "Stat", "Record")
        records.append({
            "scope": scope,
            "stat": stat,
            "source_heading": heading,
            "source_rank": rank,
            "player_name_source": player,
            "player_name": source_name_to_display(player),
            "player_url": cells[0].get("href") if cells else None,
            "source_team": first_present(raw, "TM", "Team", "Tm"),
            "value": parse_number(value_text),
            "games": parse_number(first_present(raw, "GM", "Games"), integer=True),
            "average": parse_number(first_present(raw, "Ave.", "Average", "Avg")),
            "season": parse_number(season_value, integer=True),
            "round": first_present(raw, "Round", "Rnd"),
            "opponent": opponent,
            "match_date": first_present(raw, "Date"),
            "match_description": match_description,
            "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        })
    return records


'''

FUNCTION_PATTERN = re.compile(
    r"def parse_record_fragment\(.*?\n(?=def parse_records\()",
    flags=re.S,
)


class HotfixError(RuntimeError):
    pass


def compile_file(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        check=False,
    )
    if result.returncode:
        raise HotfixError(f"compile failed: {path}")


def load_parser(path: Path, utils_dir: Path):
    sys.path.insert(0, str(utils_dir))
    try:
        sys.modules.pop("club_sources", None)
        spec = importlib.util.spec_from_file_location(
            "club_source_loader_v5_validation", path
        )
        if spec is None or spec.loader is None:
            raise HotfixError("could not load patched parser")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        try:
            sys.path.remove(str(utils_dir))
        except ValueError:
            pass


def validate_cached_pages(module) -> tuple[int, int]:
    failures: list[str] = []
    total_rows = 0
    page_count = 0

    for club in module.CLUBS:
        path = module.source_path(
            module.DEFAULT_RAW_DIR, club, "afltables_records"
        )
        if not path.exists():
            failures.append(f"{club.club_id}: missing {path}")
            continue
        try:
            rows = module.parse_records(path)
            season = sum(row.get("scope") == "season" for row in rows)
            game = sum(row.get("scope") == "game" for row in rows)
            season_stats = {
                row.get("stat") for row in rows if row.get("scope") == "season"
            }
            game_stats = {
                row.get("stat") for row in rows if row.get("scope") == "game"
            }
            bad_rows = [
                row for row in rows
                if not row.get("player_name_source") or row.get("value") is None
            ]
            if len(rows) != 840 or season != 420 or game != 420:
                raise ValueError(
                    f"expected 840 rows (420 season, 420 game), got "
                    f"{len(rows)} ({season} season, {game} game)"
                )
            if len(season_stats) != 21 or len(game_stats) != 21:
                raise ValueError(
                    f"expected 21 stats per scope, got "
                    f"{len(season_stats)} season and {len(game_stats)} game"
                )
            if bad_rows:
                raise ValueError(
                    f"{len(bad_rows)} rows are missing player or record value"
                )
            print(
                f"  {club.club_id:22} {len(rows):4} rows "
                f"({season} season, {game} game)"
            )
            total_rows += len(rows)
            page_count += 1
        except Exception as exc:  # report every club in one pass
            failures.append(f"{club.club_id}: {type(exc).__name__}: {exc}")

    if failures:
        print("\nRecord validation failures:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        raise HotfixError("cached-page validation failed")

    return page_count, total_rows


def apply(root: Path) -> int:
    root = root.resolve()
    target = root / "utils" / "load_club_sources.py"
    if not target.exists():
        print(f"error: parser not found: {target}", file=sys.stderr)
        return 1

    original = target.read_text(encoding="utf-8")
    match = FUNCTION_PATTERN.search(original)
    if not match:
        print(
            "error: parse_record_fragment function was not found; "
            "the loader differs from the expected club-data version",
            file=sys.stderr,
        )
        return 1

    patched = FUNCTION_PATTERN.sub(lambda _: NEW_FUNCTION, original, count=1)
    backup = target.with_suffix(target.suffix + ".bak_club_records_v5")
    if not backup.exists():
        shutil.copy2(target, backup)

    changed = patched != original
    if changed:
        target.write_text(patched, encoding="utf-8")
        print("patched  utils\\load_club_sources.py")
    else:
        print("unchanged utils\\load_club_sources.py")

    try:
        print("\nCompiling parser...")
        compile_file(target)
        print("Validating the actual 18 cached club pages...")
        module = load_parser(target, root / "utils")
        pages, rows = validate_cached_pages(module)
    except Exception as exc:
        if changed:
            target.write_text(original, encoding="utf-8")
            compile_file(target)
            print("restored utils\\load_club_sources.py after failed validation")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nValidated {pages} club pages and {rows:,} record rows.")
    print("\nParser hotfix installed. Rebuild the club source layer with:")
    print("  python .\\utils\\load_club_sources.py `")
    print("    --db .\\data\\afl\\afl.db `")
    print("    --report `")
    print("    --details")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Sports Data Lab project root",
    )
    args = parser.parse_args(argv)
    return apply(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
