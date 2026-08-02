# Sports Data Lab — family draft update

This optional AFL layer imports the Wikipedia father–son and father–daughter
source CSV into SQLite. The drafted player and father are resolved independently
against the men's VFL/AFL `players` table.

Only `unique` and `resolved` links are queryable. Ambiguous and unmatched names
remain in `family_draft` with their candidate counts and link notes. AFLW drafted
players are retained as `out_of_scope`; their fathers can still resolve to AFL
players.

## Install

Stop Streamlit and any database build/import process first.

```powershell
python C:\path\to\family_draft_update\apply_family_draft_update.py C:\sports_data_lab
cd C:\sports_data_lab
python -m compileall -q .
python .\test_family_draft.py
```

The installer is idempotent and creates one `.bak_family_draft` backup before
first modifying each existing project file.

## Scrape and import

```powershell
python .\scrape_wikipedia_family_draft.py
python .\load_family_draft.py --inspect
python .\load_family_draft.py
python .\load_family_draft.py --report --details
```

`data_paths.py` selects the canonical source in this order:

1. `data\afl\raw\wikipedia_family_draft.csv`
2. `data\afl\raw\family_draft.csv`

Use an explicit database when required:

```powershell
python .\load_family_draft.py --db .\gridley.db
```

A successful future `build_db.py` run automatically re-links the local family
CSV because database player IDs may change after a rebuild.

## Grid Solver builders

- `Father-son selection`
- `Father also played AFL`
- `Father played for club`
- `Parent-child pair`

The first three return the drafted son. `Parent-child pair` returns either
member of a relationship only when both people are trusted AFL links.

## Advanced Search

The installer switches Advanced Search and `search_cli.py` to the extension
wrapper while retaining the complete existing query language.

```text
father_son:true
father_played:true
father_club:Collingwood
father:"Peter Daicos"
child_of:"Peter Daicos"
parent_child:true
club:Collingwood father_son:true games>=100
```

Structured URL parameters use the same names, including `father_son=1`,
`father_played=1`, `father_club=Collingwood`, `father=Peter Daicos` and
`parent_child=1`.

## Table grain and audit fields

`family_draft` has one row per source relationship. It retains source names,
URLs, draft year, drafting club, selection details, published games figures,
Wikipedia revision, scrape timestamp, both local player IDs, both match
statuses, candidate counts, resolution notes and import timestamp.

The importer never treats a published father-games total as identity proof.
It uses normalised names plus career-generation evidence, and uses the drafting
club only when linking the drafted player. The father is not required to have
played for the son's drafting club.

## Verification

```powershell
python .\test_family_draft.py
python .\test_core_regressions.py
python .\test_query_filters.py
python .\test_repair_database.py
python .\test_captains.py
python .\test_footywire_rising_star.py
python .\test_integration.py
python .\test_awards_integration.py --db .\gridley.db
```
