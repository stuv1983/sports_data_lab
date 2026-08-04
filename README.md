# Sports Data Lab

A local-first sports database, research toolkit and puzzle-solving application.

Sports Data Lab currently focuses on Australian Football League and historical VFL/AFL data. It combines a reproducible SQLite database build, structured player and match search, statistical exploration, data-quality checks and a Gridley-compatible grid solver in one Streamlit interface.

The underlying query engine and application shell are designed to support additional sports. AFL is the implemented sport today; NBA support is planned but is not yet production-ready.

## What it does

Sports Data Lab is more than a grid solver. It provides:

- a locally built historical AFL player-game database;
- player, club, season, venue, match-stat and career-stat search;
- an advanced text query language with bookmarkable URL parameters;
- statistical leaderboards and random player discovery;
- a prefilled Grid Solver with eligible-player counts;
- historical and practice Gridley boards where supported;
- player and match exploration through Game Lab;
- database health, schema and optional-data status checks;
- draft, award, captaincy, Rising Star and family-draft extensions;
- Dark, Light and Custom appearance modes;
- CSV export and standalone SQL output.

The application reads the generated database locally. Database files and downloaded source datasets are intentionally excluded from Git.

## Current scope

| Area | Status |
|---|---|
| AFL/VFL player-game history | Supported |
| Career and season summaries | Supported |
| Derived matches and stable match IDs | Supported |
| Grid Solver | Supported |
| Player Search | Supported |
| Advanced Search | Supported |
| Stats Explorer | Supported |
| Game Lab | Supported |
| Database Health | Supported |
| Draft and recruitment data | Optional local import |
| Awards and All-Australian data | Optional local import |
| Club captaincy | Optional local import |
| Rising Star nominations | Optional local import |
| Family-draft relationships | Optional local import |
| Club metadata and records | Optional local import |
| NBA player-game history | Supported (private local build) |
| NBA Player Search, Advanced Search, Stats Explorer, Grid Solver | Supported |
| NBA Club Explorer, Past Games, Awards, Game Lab, grid library | Not implemented |
| NBA draft, awards, teammates, coaches | Not implemented |
| Daily grid, practice grid, crowd rarity | Not implemented for either sport |

Exact season coverage and row counts depend on the version of the upstream cached dataset used for the local build. The application displays live database counts in its **Database status** panel rather than relying on hard-coded numbers.

## Interface

The Streamlit application includes the following pages:

- **Home** — project overview and database summary.
- **Player Search** — find a player and inspect career information.
- **Club Explorer** — browse current-club metadata, all-time players and records.
- **Advanced Search** — combine multiple filters using a compact query language.
- **Stats Explorer** — browse and rank player statistics.
- **Random Discovery** — surface lesser-known players and records.
- **Grid Solver** — build or load a 3 × 3 player grid and inspect every valid answer.
- **Game Lab** — explore player, match and criterion combinations.
- **Database Health** — inspect schema, row counts, link quality and optional datasets.

The interface supports Dark, Light and Custom themes through `theme.py`.

## Grid Solver

The Grid Solver intersects one row criterion with one column criterion for each square.

Each populated square shows:

- the best database-ranked answer;
- the number of eligible players;
- a 0–5 star obscurity rating;
- a ranked result list when opened;
- standalone SQL for the selected square.

The star rating is a local obscurity proxy based on career footprint and era. It is **not** Gridley's live crowd rarity percentage.

Historical grids may be loaded in two modes:

- **Authentic** — only runs grids whose six original criteria are supported.
- **Practice** — may replace an unsupported criterion with a clearly labelled supported alternative.

Unsupported or incomplete criteria are reported rather than silently guessed.

## Advanced Search

Advanced Search accepts human-readable filters that compile to parameterised SQLite queries.

Examples:

```text
club:Hawthorn captain:true games>=100 sort:obscurity
captain_club:Carlton captain_year:1995..2001
club:"St Kilda" club:Brisbane played:1995..2010
game.disposals>=30 game.goals>=3 postseason:true
season.goals>=50 debut:1980..1999 sort:score limit:50
award:brownlow-medal drafted_by:Carlton
```

Important behaviour:

- repeating `club:` applies an **AND** condition;
- `club_any:` applies an **OR** condition;
- supported stat scopes include `game.`, `season.`, `career.` and `avg.`;
- all game-scoped conditions apply to the same match;
- structured URL parameters can reproduce and share searches;
- values are parameterised and only recognised fields can become SQL.

## Requirements

- Python 3.10 or newer
- Git
- SQLite support included with Python

Core Python packages:

```bash
python -m pip install streamlit pandas numpy pyreadr
```

Individual optional importers may report extra dependencies when run.

## Clone and set up

### Git Bash on Windows

```bash
git clone https://github.com/stuv1983/sports_data_lab.git
cd sports_data_lab

python -m venv .venv
source .venv/Scripts/activate

python -m pip install --upgrade pip
python -m pip install streamlit pandas numpy pyreadr
```

### PowerShell activation

```powershell
.\.venv\Scripts\Activate.ps1
```

## Build the AFL database

Run the standard build from the repository root:

```bash
python build_db.py
```

The build process:

1. downloads or reuses the cached `afldata.rda` dataset;
2. normalises player-game records;
3. derives results, opponents, finals flags and estimated birth years;
4. creates career-level player summaries;
5. builds season goal and team-season tables;
6. creates SQLite indexes;
7. derives the canonical `matches` table;
8. links player-game rows to stable match IDs;
9. re-links every optional layer that has local source files (see
   **Optional data layers** below).

Step 9 matters. A rebuild reassigns `player_id` values, so any layer that
resolves names to ids has to be re-run afterwards or it points at the wrong
people. `build_db.py` now does this itself, into the same database it just
built. Use `--core-only` to skip it, and `--db` to build somewhere else:

```bash
python build_db.py --db scratch.db      # everything lands in scratch.db
python build_db.py --core-only          # base tables only
```

### Database location

There is one canonical database path, resolved by `data_paths.py`:

```text
data/afl/afl.db
```

`gridley.db` at the repository root is the pre-`data/` legacy location. It is
still read as a fallback when no `data/afl/afl.db` exists, but nothing writes
there by default. Every script resolves the path through
`data_paths.default_db("afl")` rather than hardcoding it, so the application
and the loaders cannot drift onto different files.

Paths are anchored to the repository root, not the working directory, so the
application finds its database whichever folder it is launched from.

When both layouts are possible, `data_paths.py` resolves the active database consistently.

### Refresh the source cache

```bash
python build_db.py --refresh
```

### Use a different database path

```bash
python build_db.py --db my_afl.db
```

### Skip match derivation

```bash
python build_db.py --no-matches
```

Match derivation can then be run separately:

```bash
python derive_matches.py --db my_afl.db
```

## Build the NBA database

The NBA build is a **private, local prototype**. Read the licensing note
under [NBA.com via nba_api](#nbacom-via-nba_api) before using that source.

```bash
python build_nba_db.py --source csv                      # from CSV exports
python build_nba_db.py --source csv --seasons 2018-2023  # a subset
python build_nba_db.py --source nba_api --seasons 1946-2026
python health.py --sport nba --strict
```

The builder talks to a source adapter (`nba_source.NbaSource`) and never to
a website, so the same build works from a CSV export, from NBA.com, or from
a licensed provider added later. `--source csv` is the default because it is
the one with no conditions attached.

The build is atomic. It writes `data/nba/nba.db.building`, runs the schema,
integrity and source checks against that file, copies the current database
into `data/nba/backups/`, and only then renames the working file into place.
A failed build leaves the live database untouched, keeps `nba.db.building`
for inspection, and writes the reason to `nba.db.build-report.json`.

```bash
python build_nba_db.py --source csv --keep-backups 10   # deeper history
python build_nba_db.py --source csv --in-place          # no temp, no backup
```

### NBA data layout

```text
data/nba/nba.db                            the database
data/nba/nba.db.building                   a build in progress, or a failed one
data/nba/nba.db.build-report.json          how the last build ended
data/nba/backups/nba-YYYYmmdd-HHMMSS.db    the databases it replaced
data/nba/raw/csv/                          CSV source exports
data/nba/cache/nba_api/                    cached NBA.com responses
data/nba/reference/nba_reference.json      team list, lineage, measured eras
```

`nba_reference.json` is written by the build and read back by `sports.py` at
import time, because `core.Schema` is a frozen dataclass constructed before
any database is open. `nba_reference.py` carries checked-in fallbacks so a
clean clone imports and runs before anything is built.

**Restart Streamlit after a build that changes the team list or the measured
statistic eras.** The database-revision cache key invalidates queries, not a
frozen dataclass.

### NBA build behaviour

- The build is idempotent. Running it twice produces the same database down
  to the `player_id` values, which are assigned by sorting on the source's
  own identifier rather than by row order.
- `season` is the **start year**: `1996` means the 1996-97 season, and
  `season_label` carries the display form. `played:2005` therefore means the
  2005-06 season.
- Strict health checks run before the build is declared good: duplicate
  player-game keys, orphan rows, and career totals that disagree with the
  games they were aggregated from all fail the build. Pass `--no-strict` to
  keep a database anyway.
- Every source retrieval is recorded in `source_manifest` with a digest of
  the bytes as received, and anything the build reconciled rather than
  trusted is recorded in `source_issues`.
- `club_now` and `club_hist` are TEXT. The `franchises`, `teams` and
  `team_aliases` tables are the normalised source of truth those columns are
  derived from, and are what populate the team list and franchise lineage.
- Franchise lineage expands one way. An Oklahoma City Thunder square
  includes Seattle SuperSonics players; a Seattle SuperSonics square returns
  only players who appeared under that name.

## Run the application

```bash
streamlit run app.py
```

Streamlit will print the local application URL, normally:

```text
http://localhost:8501
```

## Optional data layers

The base database remains usable without optional datasets. Builders and filters for an optional layer are shown only when the required tables and trusted player links are available.

### Draft and recruitment

Draftguru data supports draft history, recruitment source, father-son, academy, trade, free-agency and related criteria.

`build_db.py` runs this layer automatically when a Draftguru scrape is present
under `data/afl/raw/draftguru`. To re-run it on its own:

```bash
python load_draftguru.py
python link_draft.py
python link_people.py
```

Run the three in that order: the loader writes `dg_people`, `draft`, `awards`
and `all_australian`, and the two link passes resolve them to `player_id`s.
The precise commands available may vary as import tooling is updated. Check each script's `--help` output before running it.

### Awards

The optional awards layer includes supported Draftguru records such as:

- All-Australian selections;
- Brownlow Medal;
- Coleman Medal;
- Norm Smith Medal;
- AFLCA and AFLPA awards;
- club best-and-fairest awards;
- selected state-league and under-18 medals;
- number-one National Draft selections.

Only rows linked to a database player with a trusted status are exposed to search and the solver.

### Club captaincy

Captaincy data is imported separately:

```bash
python load_captains.py
python load_captains.py --report
```

Captain filters become available only after linked captaincy rows exist.

### Rising Star nominations

The Rising Star layer supports locally saved source pages and a permission-gated live fetch path:

```bash
python fetch_footywire_rising_star.py --html-dir SAVED_PAGES --load-db
```

After written permission for live access:

```bash
python fetch_footywire_rising_star.py --permission-confirmed --load-db
```

The importer can also be run separately:

```bash
python load_rising_star.py
```

### Broad family relationships

Wikipedia's broad football-family list can be scraped and conservatively
linked for sibling, parent-child and extended-family search:

```bash
python scrape_wikipedia_families.py --report
python load_family_relationships.py --report --details
```

Advanced Search examples:

```text
family_relation:brother postseason:true sort:obscurity
related_to:"Gary Ablett" relative_club:Geelong
```

### Club metadata and records

The optional club source utility keeps exactly the 18 current AFL clubs and
loads Wikipedia metadata plus AFL Tables player totals, all-time player lists
and season/game record leaderboards from locally cached source files.

```bash
python utils/fetch_club_sources.py --report
python utils/load_club_sources.py --report --details
python tests/test_club_sources.py
```

AFL Tables automatic requests are permission-gated. The utility can print or
open the complete reviewed source manifest for browser-assisted saving, while
Wikipedia is fetched through the MediaWiki API. Run
`python utils/fetch_club_sources.py --help` for the full workflow.

### Club all-games match sources

A separate pass reads each club's AFL Tables "all games" page and reconciles
the two clubs' accounts of the same match against each other and against the
derived `matches` table:

```bash
python utils/fetch_historical_all_games.py
python utils/load_club_all_games.py --report
```

This writes `club_match_sources`, `match_details` (quarter-by-quarter scores,
attendance, scheduled time) and `club_match_source_issues`, and backfills
attendance onto `matches` where both clubs agree.

**This layer has no user interface yet.** The data loads and is queryable
through the CLI and SQL, but no application page reads it. Nothing depends on
it, so it is safe to skip.

### Family-draft relationships

Family-draft data is loaded and linked separately:

```bash
python load_family_draft.py
python load_family_draft.py --report --details
```

Relationship rows outside the senior-game dataset remain explicitly marked as out of scope rather than being forced onto an incorrect player.

## Data integrity

Sports Data Lab uses conservative linking rules.

- Player names are not treated as globally unique identifiers.
- Internal `player_id` values are used wherever possible.
- Optional records are exposed only when they resolve uniquely or have been explicitly resolved.
- Ambiguous and unmatched source rows are retained for reporting but excluded from answers.
- Match identity is based on a stable `match_key`.
- Existing match IDs are reused during refreshes.
- The Streamlit application opens its database in read-only mode.
- Missing optional tables degrade to unavailable features rather than application failures.
- Query values are parameterised to prevent user input becoming arbitrary SQL.

## Statistics and historical caveats

The source dataset is not equally detailed across every era.

- Goal data extends further back than most other player statistics.
- Disposals, marks, tackles and several related statistics are generally available only from 1965 onward.
- Pre-statistical-era rows may contain goals but leave later statistics empty.
- Empty historical values are not automatically treated as zero.
- Quarter-by-quarter scores and attendance are not derivable from the current player-game source and remain available for later enrichment.
- Historical club identities and current club lineages are stored separately where required.

Search results should therefore be interpreted in the context of the statistic's recorded era.

### NBA statistic eras

Basketball statistics begin in different seasons, and the database records
this rather than papering over it:

| Statistic | First recorded |
|---|---|
| Points | 1946-47 |
| Rebounds | 1950-51 |
| Minutes | 1951-52 |
| Steals, blocks, turnovers, offensive/defensive rebounds | 1973-74 |
| Three-point field goals | 1979-80 |
| Plus/minus | 1996-97 |

Cells before a statistic's era are **NULL, never 0**, in `games` and in the
career totals derived from them. A zero would be a claim about the players
rather than about the records: it would make "recorded no steals" and
"steals were not recorded" the same answer, and it would rank the entire
early league as maximally obscure for a reason that is an artefact of
record-keeping. The obscurity model drops a term it has no data for and
renormalises the remaining weights instead of reading the gap as zero.

The table above is the shape of the data, not the authority. The authority
is the `stat_coverage` table, measured from the built database by
`load_stat_coverage.py --sport nba` and read back through
`data/nba/reference/nba_reference.json`.

## Data sources

### NBA.com via nba_api

`nba_source_api.py` reads NBA.com's statistics endpoints through the
community `nba_api` package.

**This adapter is for a private, local prototype only.** NBA.com's terms
permit statistics to be used for private, non-commercial purposes and
require attribution, but they do not clear offering a comprehensive,
regularly updated NBA statistics database through a website or service
without prior consent. A database built by this adapter is fine to hold and
query on your own machine and is **not** cleared for redistribution or for
backing a hosted application.

`nba_api` is a community package and the endpoints it uses are undocumented
and change without notice, so this adapter will break without warning. That
is why `build_nba_db.py` is written against `nba_source.NbaSource` rather
than against a website: when the endpoints move, or when a licensed source
becomes available, only the adapter changes.

Every response is cached under `data/nba/cache/nba_api/` and is never
re-requested unless `--refresh` is passed, which keeps the request count to
the minimum the data requires and lets the application work offline once the
cache is warm.

### AFL Tables

AFL Tables is the original source of the historical player-game statistics.

This project does **not** scrape AFL Tables directly. Its automated-client restrictions are respected.

### fitzRoy and fitzRoy_data

`build_db.py` downloads the community-maintained cached AFL Tables player-stat dataset published by the fitzRoy project. A single cached dataset replaces a large number of individual page requests.

### Draftguru

Used by optional local importers for draft, recruitment and award history.

### FootyWire

Used for Rising Star nomination history. Live fetching is permission-gated. Manually saved pages may be parsed locally for personal use.

### Gridley

Gridley inspired the original grid-solving workflow. Sports Data Lab is an unaffiliated research and puzzle-support tool. It does not reproduce Gridley's live crowd selection percentages.

See [`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) for source credits, terms and reuse notes.

## Repository layout

Selected files:

| File | Purpose |
|---|---|
| `app.py` | Streamlit application, navigation and Grid Solver |
| `core.py` | Sport-agnostic schema, query engine and result ranking |
| `sports.py` | Sport registry, schemas and vocabulary |
| `constraints.py` | AFL-specific constraint builders |
| `advanced_search.py` | URL-addressable advanced player search |
| `query_filters.py` | Query parser and SQL compiler |
| `explore.py` | Home, player search, leaderboards and discovery pages |
| `game_lab.py` | Player and criterion exploration |
| `health.py` | Database health and integrity page |
| `theme.py` | Dark, Light and Custom themes |
| `build_db.py` | Base AFL SQLite database builder |
| `obscurity.py` | Sport-agnostic, term-driven obscurity model |
| `constraints_nba.py` | NBA-specific constraint builders |
| `build_nba_db.py` | NBA SQLite database builder |
| `nba_source.py` | NBA source-adapter contract and the CSV adapter |
| `nba_source_api.py` | NBA.com adapter (private local prototype only) |
| `nba_reference.py` | NBA team list, franchise lineage and measured eras |
| `derive_matches.py` | Canonical match table and stable match IDs |
| `repair_database.py` | Non-download database repair workflow |
| `data_paths.py` | Single source of truth for every database and data path |
| `awards.py` | Award and recruitment constraints |
| `captains.py` | Club captaincy constraints |
| `rising_star.py` | Rising Star nomination constraints |
| `historic_grids.py` | Captured-grid validation and practice support |
| `parse_criteria.py` | Grid criterion parser |
| `club_explorer.py` | Current-club metadata and records page |
| `utils/fetch_club_sources.py` | Cache current-club source pages |
| `utils/load_club_sources.py` | Parse and load club metadata and records |
| `utils/load_club_all_games.py` | Reconcile per-club match sources (no UI yet) |
| `utils/clean_project.py` | Review and remove generated artefacts |
| `utils/optimise_database.py` | Propose and create missing indexes |
| `tests/` | Every test, runnable under pytest or standalone |
| `ACKNOWLEDGEMENTS.md` | Data-source credits and reuse notes |

Database files, caches, downloaded pages, generated SQL and third-party source datasets are excluded through `.gitignore`, as is `archive/`.

## Validation and tests

Compile the Python source:

```bash
python -m compileall -q .
```

Every test lives in `tests/` and the whole suite runs under pytest:

```bash
python -m pytest -q
```

Each file is also runnable on its own, which prints a per-check report:

```bash
python tests/test_core_regressions.py
python tests/test_query_filters.py
python tests/test_club_sources.py
```

### The clean-build gate

`tests/test_integration.py` rebuilds a complete database from the cached
`.rda` and runs the release checks against it. That takes minutes, so pytest
skips it by default. Opt in explicitly:

```bash
SDL_INTEGRATION=1 python -m pytest -q      # bash
$env:SDL_INTEGRATION=1; python -m pytest -q   # PowerShell
```

Or run it directly, which is the usual way:

```bash
python tests/test_integration.py
python tests/test_awards_integration.py
```

It builds `test_gridley.db` in the repository root rather than touching the
live database. Review the command-line options of any test before pointing it
at a database you need to retain.

Advanced Search has a live smoke test that runs real queries, read-only,
against the actual database:

```bash
python tests/test_advanced_search_live.py
```

## Clean-up

`utils/clean_project.py` reviews generated artefacts and legacy files. It is a
dry run unless `--apply` is supplied.

```bash
python utils/clean_project.py --root .
python utils/clean_project.py --root . --apply
```

Database snapshots are retained unless deletion is explicitly requested.

Superseded material — one-off hotfix installers, `*.bak_*` backups, replaced
databases and completed migration tools — is moved to `archive/` rather than
deleted. That directory is gitignored and can be emptied once a release is
tagged and confirmed.

## Privacy and redistribution

The repository contains code, not a prebuilt sports-data distribution.

The following remain local and are ignored by Git:

- SQLite databases;
- the cached `.rda` source dataset;
- scraped or manually saved source pages;
- generated CSV data;
- generated SQL;
- local secrets and editor configuration.

Build your own database from the documented sources and follow each source's terms.

## Known limitations

- The AFL is the more complete implementation. The NBA has a database and the
  research pages; it has no Club Explorer, Past Games, Awards page, Game Lab,
  captured grid library or criterion parser.
- The NBA build is a private local prototype. It is not cleared for
  redistribution or for backing a hosted application.
- NBA seasons are stored as start years, so `played:2005` means 2005-06.
- There is no NBA teammate constraint. `core.Generic.teammate_of_id` matches
  on a shared team and season, which the NBA's trade window makes wrong often
  enough to matter; answering it properly needs shared matches, which is not
  built. It is absent rather than present and wrong.
- Obscurity scores from the two sports are not comparable. They come from
  different models with different terms, which is what the model version
  stored alongside every score records.
- Optional source coverage can be incomplete.
- Name matching cannot safely resolve every historical record.
- Some historical grids contain unsupported or partially captured criteria.
- The obscurity rating is heuristic and is not an official rarity score.
- Historical statistics are limited by the era in which each statistic was recorded.
- The project does not currently provide a hosted database or hosted application.

## Contributing

Before submitting a change:

1. keep sport-agnostic behaviour in `core.py` where practical;
2. keep AFL-only rules in the AFL constraint and importer modules;
3. preserve parameterised SQL;
4. do not commit databases or third-party source data;
5. add or update regression tests;
6. run the relevant test suite;
7. document new data sources and their usage restrictions.

## Disclaimer

Sports Data Lab is an independent, unofficial hobby and research project. It is not affiliated with the AFL, its clubs, Gridley, AFL Tables, fitzRoy, Draftguru or FootyWire.

Club names, competition names and award names may be trademarks of their respective owners.
