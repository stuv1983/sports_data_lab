# Sports Data Lab — speed and cleanup update

Prepared for the 2 August 2026 AFL release baseline.

## Files in this bundle

### Replacements

- `app.py`
  - Adds a database revision key based on SQLite file modification time and size, so Streamlit caches invalidate after a rebuild or refresh.
  - Tunes the read-only SQLite connection for the application's read-heavy workload.
  - Caches expanded Grid Solver result lists.
  - Reuses the board square's existing eligible count instead of running a second count query when a square is opened.
  - Removes the remaining deprecated `use_container_width=True` call.

- `explore.py`
  - Caches player profiles, captain appointments, season tables, biggest-game tables and leaderboard results by database revision.
  - Invalidates those results automatically after the database file changes.

- `advanced_search.py`
  - Caches compiled search results by SQL, parameters and database revision.
  - Ensures both captain and Rising Star placeholder tables are available on clean core databases.
  - Avoids rewriting unchanged URL parameters.

- `constraints.py`
  - Corrects the stale `fetch_draft.py` availability description. No query behaviour is changed.

### Maintenance tools

- `clean_project.py`
  - Dry-run by default.
  - Removes generated test databases, completed installer/hotfix material, obsolete validation notes, installer backups, `__pycache__` and `.pyc` files.
  - Retains release rollback databases unless `--delete-db-backups` is supplied.
  - Retains the Rising Star parser repair evidence unless `--delete-repair-files` is supplied.
  - Migrates legacy root-level captain files/cache to the canonical `data/afl` layout with SHA-256 verification.

- `optimise_database.py`
  - Dry-run by default.
  - Inspects existing indexes by column prefix and proposes only missing access paths.
  - Supports the core `games`, ladder and season-goal tables plus captain, Rising Star, draft and award layers.
  - Runs `ANALYZE` and `PRAGMA optimize` after applying changes.

- `AFL_DATA_GAPS.csv`
  - Replaces the stale backlog that still marked captaincy and Rising Star nominations as missing.
  - Prioritises family relationships, player details, AFLCA coaches' votes and match enrichment.

- `apply_speed_cleanup.py`
  - Validates the extracted bundle, compiles its Python files and installs replacements with one `.bak_speed_cleanup` backup per changed file.

## Installation

Stop Streamlit and any import or refresh process first.

```powershell
cd C:\sports_data_lab
python C:\path\to\sports_data_lab_speed_cleanup\apply_speed_cleanup.py C:\sports_data_lab
python -m compileall -q .
```

Run the release tests before cleanup or database changes:

```powershell
python .\test_core_regressions.py
python .\test_query_filters.py
python .\test_repair_database.py
python .\test_captains.py
python .\test_footywire_rising_star.py
python .\test_integration.py
python .\test_awards_integration.py --db .\gridley.db
```

## Cleanup sequence

Start with the dry run:

```powershell
python .\clean_project.py
```

Review every displayed path, then apply the safe cleanup:

```powershell
python .\clean_project.py --apply
```

After manual application acceptance and a tagged release, the retained evidence can be removed explicitly:

```powershell
python .\clean_project.py --apply --delete-db-backups --delete-repair-files
```

Do not use the final command until the release has been committed, tagged and launched successfully.

## Database optimisation sequence

Start with the dry run:

```powershell
python .\optimise_database.py --db .\gridley.db
```

Review the proposed indexes, then apply them while Streamlit is stopped:

```powershell
python .\optimise_database.py --db .\gridley.db --apply
```

Run the complete validation suites again after index creation. Indexes should not alter results, but the release gate should prove that.

## Validation completed for this bundle

- Every supplied Python file compiles successfully.
- The cleanup tool was tested in dry-run, normal apply and explicit opt-in deletion modes against a temporary project tree.
- The optimisation tool was tested against a temporary SQLite schema, created only missing indexes, and proposed zero changes on its second run.
- The complete Sports Data Lab integration suites were not runnable here because the full repository and `gridley.db` were not supplied in this environment. Run the commands above in `C:\sports_data_lab` before accepting the update.
