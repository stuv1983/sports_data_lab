# Sports Data Lab

A local-first sports database, research toolkit and puzzle-solving application.

Sports Data Lab combines a reproducible SQLite database build, structured player and match search, statistical exploration, data-quality checks and a Gridley-compatible grid solver in one Streamlit interface.

The application supports **AFL/VFL** (primary and most complete), **NBA** (full support with incremental live updates), **MLB** (via Lahman database with incremental appends), and **NFL** (1999 onward via nflverse with incremental appends). A common query engine and application shell serve all four sports; sport-specific extensions (constraints, grid criteria, award data, captaincy) live in their own packages.

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
| **AFL/VFL** — player-game history | Supported; optional draft, awards, captaincy, Rising Star, family data |
| **AFL** — Career and season summaries | Supported |
| **AFL** — Club metadata, records, all-time players | Optional local import from Wikipedia/AFL Tables |
| **AFL** — Grid Solver | Supported; captured grids; criterion parser; practice mode for unsupported axes |
| **AFL** — Game Lab | Supported (player, match, career-stat modes) |
| **All sports** — Player Search | Supported; accented names, fuzzy matching |
| **All sports** — Advanced Search | Supported; compact query language with URL parameters |
| **All sports** — Stats Explorer | Supported; leaderboards and discovery |
| **All sports** — Database Health | Supported; schema, row counts, optional-data status |
| **All sports** — Grid Solver | Supported; find eligible players, ranked by obscurity |
| **All sports** — Club Explorer | Supported (current-club metadata per-sport) |
| **All sports** — Past Games | Supported; match search and filtering |
| **All sports** — Awards page | Supported (award lists per-sport) |
| **All sports** — Game Lab | Supported (generic modes for all sports) |
| **NBA** — player-game history | Supported (private local build, incremental live updates) |
| **NBA** — Automated updates | Incremental current-season append from NBA.com via `load_current_season.py` |
| **NBA** — draft, teammates, coaches, grid library | Not implemented |
| **MLB** — career history | Supported via Lahman database CSV export |
| **MLB** — Automated updates | Incremental season append from Stats API via `load_statsapi.py` |
| **MLB** — single-game squares | Not possible (Lahman grain is player-season, not per-game) |
| **MLB** — draft, awards, grid library | Not implemented |
| **NFL** — player-game history | Supported, 1999 onward via nflverse (weekly data) |
| **NFL** — Automated updates | Incremental weekly data fetch via nflverse |
| **NFL** — snap counts, injuries, depth charts, contracts, trades | Imported with `--extended`; no game squares use them yet |
| **NFL** — draft squares | Supported |
| **NFL** — pre-1999 careers | Not possible (nflverse data begins 1999) |
| **NFL** — grid library | Not implemented |
| **Cross-sport** — Dark, Light, Custom themes | Supported |
| **Cross-sport** — CSV export, standalone SQL | Supported |
| **Cross-sport** — Daily grid, practice grid, crowd rarity | Not implemented |

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

### Finding a player

Every page that asks for a player uses the one picker in `ui_widgets.py`, and
it does two things worth knowing about:

- **Matches appear as you type.** The picker holds the sport's most-played
  players in the browser and filters them keystroke by keystroke, with no
  round trip. `QUICK_PLAYER_LIMIT` sets how many; raise it for a shorter tail
  at the cost of a larger page.
- **A name that means one player is not asked about twice.** Type a full name
  and it resolves outright — no second dropdown. Only a genuinely ambiguous
  query ("acuna", with two Acuñas) asks you to choose, and two players who
  really do share a name always will.

Typing a name the browser was not given — a four-game career, an accented
spelling, a typo — searches every player in the database instead, with the
accent-folding and similarity rules described in `player_matches`. So the cap
above changes how fast a name appears, never whether it can be found.

### Application icons

Drop PNGs into `static/icons/` to set the browser tab icon and the icon iOS
uses when the app is saved to a home screen. Per sport (`nba.png`,
`nba-180.png`) or for all of them (`default.png`, `default-180.png`); see
[`static/icons/README.md`](static/icons/README.md). With the folder empty each
sport keeps the emoji from its registry entry, so nothing needs to be added.

`branding.py` handles this. The tab icon goes through Streamlit; the
home-screen icon does not, because Streamlit renders the `<head>` itself and
offers no way in, so the tags are written from the browser instead.

## Grid Solver

The Grid Solver intersects one row criterion with one column criterion for each square.

Each populated square shows:

- the best database-ranked answer;
- the number of eligible players;
- a 0–5 star obscurity rating;
- a ranked result list when opened;
- standalone SQL for the selected square.

The star rating is a local obscurity proxy based on career footprint and era. It is **not** Gridley's live crowd rarity percentage.

Boards can be built by hand, generated, pasted in criterion by criterion,
loaded from a saved or captured grid, or — for the AFL — opened straight from
[gridleygame.com](https://gridleygame.com) with **Today's Gridley**. That
source reads a day's board from the captured library when the scheduled scan
has already stored it, and asks the site directly for any day it has not
reached yet, so the current board is playable before the next scan runs.

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

Install the application, importer, and test dependencies:

```bash
python -m pip install -r requirements.txt
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
python -m pip install -r requirements.txt
```

### PowerShell activation

```powershell
.\.venv\Scripts\Activate.ps1
```

## Build the AFL database

Run the standard build from the repository root:

```bash
python -m afl.build_db
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
people. `afl/build_db.py` now does this itself, into the same database it just
built. Use `--core-only` to skip it, and `--db` to build somewhere else:

```bash
python -m afl.build_db --db scratch.db      # everything lands in scratch.db
python -m afl.build_db --core-only          # base tables only
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
python -m afl.build_db --refresh
```

### Use a different database path

```bash
python -m afl.build_db --db my_afl.db
```

### Skip match derivation

```bash
python -m afl.build_db --no-matches
```

Match derivation can then be run separately:

```bash
python -m afl.derive_matches --db my_afl.db
```

### Enter a round the source dataset has not published yet

The AFL build reads the cached fitzRoy dataset rather than scraping AFL Tables,
because [afltables.com](#afl-tables)' robots.txt disallows automated clients on
the stats paths. That dataset lags the live season, so a round can be played
and still be missing. `utils/afl/load_round_csv.py` closes the gap from the one
source that needs no crawler: the AFL Tables match pages, copy-pasted into
CSVs.

Put one folder per round together, holding the season page's rows for that
round (two lines per match) and one file per match holding that match page's
four tables — both sides' *Match Statistics*, then both sides' *Player
Details*. Then either browse to it:

```bash
python -m utils.afl.load_round_gui
```

or name it on the command line:

```bash
python -m utils.afl.load_round_csv --dir ./rd23 --season 2026 --round 23 --dry-run
python -m utils.afl.load_round_csv --dir ./rd23 --season 2026 --round 23
python -m utils.shared.recompute_obscurity --sport afl   # if anyone debuted
```

Always check first — `--dry-run`, or the **Check** button. It reads everything,
resolves every name and writes nothing, so the report is the review step.

**The browse window.** These CSVs are written by hand and live wherever suits
at the time, so the folder is a setting rather than a fixed path under `data/`.
`load_round_gui.py` is a browse window over the same loader: pick the folder,
and it reports what it found, offers the round summary it thinks you mean, and
fills in the season and round from the summary's dates and the folder's name.
**Check** and **Load** run the loader on a worker thread and stream its output
into the log pane, so a load that takes a minute does not freeze the window.

The folder is remembered as soon as one has been checked, in
`data/app/round_loader.json`, and the command line reads the same setting —
so once it has been chosen in either place, `--dir` can be left off:

```bash
python -m utils.afl.load_round_csv --season 2026 --round 24 --dry-run
```

Game files are paired to fixtures by the club names in their *Match Statistics*
headings, never by filename — a hand-assembled folder collects misnamed and
duplicated copies, and pairing on names means a stale rename cannot load a
match twice or attach stats to the wrong fixture. Anything else in the folder
is ignored and named in the report. Where more than one file could be the round
summary, pass `--summary Rd23.csv`.

Three checks have to pass before anything is written.

Each side's player goals must add up to the quarter scores the summary states.

Every name must resolve to exactly one player. Names are not unique — 460 names
in `afltables_player_index` belong to more than one player — so a name matching
several people is settled first by era (a 2026 match was not played by someone
who last played in 1937, which is what separates the two Archie Robertses) and
then by club (which separates the two Bailey Williamses playing right now, one
at the Western Bulldogs and one at West Coast). A name that survives both is
reported as ambiguous and stops the load rather than being guessed.

Every player's stated career games must be one more than the database holds.
AFL Tables counts the match being read, so a player with 174 games is listed at
175 and a debutant at 1. That makes the Player Details column an independent
check on the identity decision — it catches a misspelled name that would
otherwise be created as a new player and split a real career in two, and it
catches a match to the wrong namesake from the other direction.

**A player's first game.** A name that matches nobody is a debut: it gets a new
`player_id` and a `players` row. The date of birth is exact rather than
estimated, being the match date less the age the Player Details table states,
so `18-Nov-2005` comes out of "20y 285d" on 9 August 2026 without needing a
source that carries birthdays. Career totals are counted from the games rows
themselves, and obscurity is left unscored for `recompute_obscurity`, which is
the only thing that should write it — so run that after a round with a debut in
it, or the new players rank as maximally obscure until you do.

The career-games check is what keeps this honest. A debut is only accepted when
AFL Tables agrees it is the player's first game; if the source says career game
115 and the database holds none, the load stops rather than inventing a
115-game rookie.

**Surviving a rebuild.** `afl/build_db.py` replaces `games` and `players`
wholesale, which would drop a hand-entered round. The parsed rows are therefore
kept in `manual_round_games`, and the fixtures in `club_match_sources` beside
the [All Games](#club-all-games-match-sources) observations. `afl/build_db.py`
re-applies them itself at the end of every rebuild, so a scheduled refresh
cannot quietly lose a round. Rounds the rebuild has now produced upstream are
skipped, so this stops mattering by itself once the dataset catches up:

```bash
python -m utils.afl.load_round_csv --apply-only     # after a manual rebuild
python -m utils.afl.load_round_csv --forget 2026 23 # once fitzRoy carries it
```

## Automated database updates

The `database_updates.py` script runs on a schedule and keeps the AFL, NBA, and MLB databases current without rebuilding them from scratch. A **"regular"** update runs frequently (every ~7 days for AFL, ~5 months for NBA, etc.) and appends/refreshes only the most recent season's games. A **"full"** rebuild is manual and rare (after a source format change or a deliberate reset):

```bash
python database_updates.py              # automated regular update
python database_updates.py --full       # manual full rebuild (rare)
python database_updates.py --dry-run    # preview without writing
```

**MLB and NBA no longer rebuild from scratch in automated updates.** Instead:

- **MLB**: appends/replaces the current season from the Stats API via `utils/mlb/load_statsapi.py`
- **NBA**: appends/replaces the current seasons from NBA.com via `utils/nba/load_current_season.py`

This preserves historical team identities (e.g., Seattle SuperSonics for 1985, not Oklahoma City Thunder), ensures career totals stay accurate, and keeps the updates fast. A full rebuild is still possible manually when needed (e.g., after importing a new source dataset).

**AFL** continues to rebuild from the cached fitzRoy dataset every run because it is small and reliable; a rebuild is fast and catches edge cases reliably.

## Build the NBA database

The NBA build is a **private, local prototype**. Read the licensing note
under [NBA.com via nba_api](#nbacom-via-nba_api) before using that source.

```bash
# Full rebuild (manual, rare)
python -m nba.build_nba_db --source csv                      # from CSV exports
python -m nba.build_nba_db --source csv --seasons 2018-2023  # a subset
python -m nba.build_nba_db --source bbr --source-root C:/nbaData/data
python -m nba.build_nba_db --source nba_api --seasons 1946-2026  # caution: see licensing note below

# Incremental update (automated, regular)
python -m utils.nba.load_current_season --db data/nba/nba.db
python -m utils.nba.load_current_season --db data/nba/nba.db --dry-run

python health.py --sport nba --strict
```

**Full rebuilds** work from a CSV export, Basketball-Reference scrape, NBA.com live data, or other sources. The builder uses a source adapter (`nba_source.NbaSource`) and never talks to a website directly. `--source csv` is the default because it is the one with no conditions attached.

**Automated updates** (run by `database_updates.py` on schedule) use `utils/nba/load_current_season.py`, which appends/refreshes only the current season(s) from the live source. This preserves historical accuracy and keeps updates fast, since a franchise's current season is always played under its current name (no historical-identity mismatch). See [Automated database updates](#automated-database-updates) above for context.

### Build from the Basketball-Reference scrape

`nba/nba_source_bbr.py` reads the local scrape: `games.csv` and `players.csv`
plus one JSON box score per game. Read the licensing note under
[Basketball-Reference](#basketball-reference) first.

```bash
python -m nba.check_nba_scrape --verbose        # how far has the scrape got?
python -m nba.build_nba_db --source bbr --source-root C:/nbaData/data
python -m nba.build_nba_db --source bbr --complete-only   # skip unscraped games
```

Set `NBA_SCRAPE_ROOT` and `--source-root` can be dropped. `data/nba/sample`
works as a root as-is, which is what the adapter's tests run against.

A scrape in progress is a legitimate input: the index lists every game long
before the box scores land, so a game with no JSON builds as a fixture
nobody has stats for. `--complete-only` narrows the schedule to games whose
box score has arrived, which is the honest way to build a partial database —
without it, career totals are short by however much has not been scraped
yet, and nothing in the database says so.

The ABA is excluded by default. Its nine championships are not NBA
championships, and folding them in would make "champion" wrong for nine
seasons; `--include-aba` builds them anyway.

The build is atomic. It writes `data/nba/nba.db.building`, runs the schema,
integrity and source checks against that file, copies the current database
into `data/nba/backups/`, and only then renames the working file into place.
A failed build leaves the live database untouched, keeps `nba.db.building`
for inspection, and writes the reason to `nba.db.build-report.json`.

```bash
python -m nba.build_nba_db --source csv --keep-backups 10   # deeper history
python -m nba.build_nba_db --source csv --in-place          # no temp, no backup
```

### NBA data layout

```text
data/nba/nba.db                            the database
data/nba/nba.db.building                   a build in progress, or a failed one
data/nba/nba.db.build-report.json          how the last build ended
data/nba/backups/nba-YYYYmmdd-HHMMSS.db    the databases it replaced
data/nba/raw/csv/                          CSV source exports
data/nba/raw/bbr/                          the Basketball-Reference scrape,
                                           or $NBA_SCRAPE_ROOT
data/nba/sample/                           a slice of the scrape, checked in
data/nba/cache/nba_api/                    cached NBA.com responses
data/nba/output/                           saved reference pages, as retrieved
data/nba/reference/nba_reference.json      team list, lineage, measured eras
data/nba/reference/playoff_series.csv      bracket structure, 1946-2025
```

### Playoff rounds

Box-score sources say a game was a playoff game. They do not say which
round, and without the round four criteria answer nobody: Finals appearance,
Finals win, appeared for the championship team, and the championship
derivation itself. The build records that gap as an error rather than
guessing at it.

`reference/playoff_series.csv` fills it. It is generated once from a saved
copy of Basketball-Reference's playoff-series index and read at build time;
nothing in the build touches the network.

```bash
python -m utils.nba.load_nba_playoff_series --check  # verify, write nothing
python -m utils.nba.load_nba_playoff_series          # write reference file
```

The loader checks its own output before writing: every series name must
classify to a known round, every season must contain exactly one Finals, no
two teams may meet twice in one season, and every derived champion is
cross-checked against the champion column of a separately saved league
index. All 979 series and all 89 champions from 1946-47 to 2025-26 pass.

A game is matched to its series by season and team pair, using the
*historical* team identity — the reference names Seattle SuperSonics, not
Oklahoma City Thunder. A playoff game with no matching series is left
without a round and fails the strict build.

BAA championships count as NBA championships, because the league counts
them. ABA championships do not, and the loader keeps the leagues apart.

Basketball-Reference is used here for verification and bracket structure,
not as an import source: its terms do not clear a comprehensive database for
redistribution, so the saved pages and everything derived from them stay
under `data/`, which is not tracked.

`nba_reference.json` is written by the build and read back by `sports.py` at
import time, because `core.Schema` is a frozen dataclass constructed before
any database is open. `nba/nba_reference.py` carries checked-in fallbacks so a
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

## Build the MLB database

The MLB build reads the [Lahman baseball database](#lahman-baseball-database) CSV export. Put the extracted CSVs under `data/mlb/raw/` (or drop the `lahman_*_csv.zip` in there and leave it zipped):

```bash
# Full rebuild (manual, rare)
python -m mlb.build_mlb_db                       # -> data/mlb/mlb.db
python -m mlb.build_mlb_db --raw path/to/csv     # a different export
python -m mlb.build_mlb_db --db /tmp/mlb.db      # a different output

# Incremental update (automated, regular)
python -m utils.mlb.load_statsapi --db data/mlb/mlb.db
python -m utils.mlb.load_statsapi --db data/mlb/mlb.db --dry-run

python health.py --sport mlb --strict
```

**Full rebuilds** use the Lahman CSV export to establish a complete, historically accurate database. **Automated updates** (run by `database_updates.py` on schedule) use `utils/mlb/load_statsapi.py`, which appends/replaces only the current season from the Stats API. This mirrors the [Automated database updates](#automated-database-updates) pattern described above.

The build reads `People`, `Appearances`, `Batting`, `Pitching`, their
`*Post` counterparts, `Teams`, `TeamsFranchises`, `SeriesPost`,
`AwardsPlayers` and `HallOfFame`, and writes `data/mlb/mlb.db` plus the
measured franchise list and stat eras in
`data/mlb/reference/mlb_reference.json`. Restart Streamlit after a build
that changes the franchise list: `sports.py` freezes it into a dataclass at
import, so the query cache key does not invalidate it.

### A row of `games` is a season, not a game

Lahman's finest grain is a player's season with one team. It has no box
scores, so an MLB row of `games` is a player-season-team carrying that
season's totals, and the repository is deliberate about this rather than
quiet:

- `games` on the row is the appearance count the season is worth, so
  `career_games` still sums to a real number of games played. It comes from
  `Appearances.G_all`, not from the batting line, so a relief pitcher's 60
  games are 60 games.
- `constraints_mlb.py` does **not** offer the per-game squares -- "X+ of a
  stat in one game", "two stats in the same game", per-game averages, or
  teammates. A per-season total behind a per-game label would silently turn
  "40 home runs in a game" into a routine season, and a missing square is
  better than a wrong one. `tests/test_mlb_build.py` fails if one is added
  back.
- The Database status panel labels the count **Player-seasons**, not
  Player-games.
- `career_game_no` numbers a player's seasons, so "first career game for
  club" means "debuted for this club" -- the same question at this grain.

### Franchises and the postseason

`club_hist` is the team's name in that season ("Brooklyn Dodgers") and
`club_now` is the franchise's current name ("Los Angeles Dodgers"), which is
the same convention the AFL and NBA builds use. The mapping is measured from
Lahman's `franchID` rather than hand-maintained, and franchise lineage is
one-directional: a Dodgers square includes Brooklyn, a Brooklyn square does
not include Los Angeles.

`BattingPost`, `PitchingPost` and `SeriesPost` give each postseason row a
real `round` (`WS`, `ALCS`, ...) and a real `result`, so "won a final" means
won a postseason series and "Won the World Series" is a distinct square.
Players whose entire career fell in seasons with no postseason carry NULL
rather than 0 for `postseason_played`, and the obscurity model drops the
term for them instead of scoring them as having failed to reach an October
that did not exist.

### MLB statistic eras

Every stat in the current export is recorded from 1871, which the build
measures rather than assumes. That is not the same as full coverage:
thousands of 19th-century batting lines leave RBI, stolen bases and
strikeouts NULL even though the columns exist. An era cutoff cannot express
"recorded, patchily", so the caveat is carried in the sport's empty-square
hint instead.

## Build the NFL database

The NFL build downloads [nflverse](#nflverse) data through `nflreadpy`. It runs in two steps: a builder that imports nflverse faithfully, then an adapter that derives the columns the application reads.

```bash
pip install nflreadpy
python -m nfl.build_db --all-history --replace              # -> data/nfl/nfl.db
python -m nfl.build_db --all-history --extended --replace   # + 8 more optional datasets
python -m utils.nfl.patch_nfl_db                            # adapter step (required)
python -m utils.shared.recompute_obscurity --sport nfl      # recompute career scores

# Incremental updates are handled by nflreadpy's own schedule
python -m nfl.build_db --all-history --replace --dry-run    # check for new data
```

`--replace` is not optional once a database exists. Without it the builder
downloads everything, then refuses at the final step and deletes its own
working file, which looks exactly like a crash after fifteen minutes of
downloading.

`--extended` adds weekly rosters, snap counts, injuries, depth charts,
officials, combine results, contracts and trades. All eight are optional
*inside* the build: one can fail without failing the build, recording the
reason in `build_warnings`. The Database status panel lists every one of them
with its row count or `not loaded`, so a skipped dataset is visible rather
than silently absent.

### The adapter step

`nfl/build_db.py` writes nflverse's own tables. `utils/nfl/patch_nfl_db.py` derives
what `core.py` asks every sport for and nflverse does not carry: `club_hist`
and `club_now` (from the team catalogue and the code map in
`nfl/nfl_reference.py`), `date`, `venue`, `round` and `result` (from
`matches`), `is_playoff`, `career_game_no`, and a `touchdowns` column. It also
restates `teams_hist` and `n_teams` in franchise names, so a player who moved
with the Raiders is one team rather than two.

Everything it writes derives from `games`, so it is rerunnable, and
`--dry-run` reports without writing. Run it after every build: a rebuild
replaces the database file and takes the derived columns with it. Until it has
run, the app declines to load the sport and says which command to run.

### Weekly statistics begin in 1999

nflverse's schedules and rosters reach back to 1920, but its weekly player
statistics start in 1999, and `games` is built from those. So:

- `career_games`, `career_touchdowns` and every other career total are
  1999-onward figures, not whole NFL careers.
- `career_game_no` counts from a player's first game *in that data*: a 1994
  debut is numbered from 1999.
- `rosters` knows a player was on a 1955 roster. That is not an appearance,
  and no square treats it as one.
- The player list holds 25,041 identities, of which 11,372 have a game.
  Obscurity is a percentile rank, so scoring the other 13,669 would tie 55% of
  the table at the top and push every player who did play down the scale;
  `sports.NFL.obscurity_population` restricts scoring to players with a game,
  and the status panel reports both counts.

`touchdowns` and `career_touchdowns` are touchdowns *responsible for*,
passing included -- which is not the usual "touchdowns scored".

### NFL statistic names

The stat list offered as grid axes is curated in `nfl/nfl_reference.py` from
the ~130 numeric columns the weekly dataset carries, and it uses nflverse's
own names. Those names change: `interceptions` and `sacks` no longer exist and
are now `passing_interceptions` and `def_sacks`. After an `nflreadpy` upgrade,
`utils/nfl/patch_nfl_db.py` reports any declared statistic that the built `games`
table no longer has.

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

`afl/build_db.py` runs this layer automatically when a Draftguru scrape is present
under `data/afl/raw/draftguru`. To re-run it on its own:

```bash
python -m utils.afl.load_draftguru
python -m afl.link_draft
python -m afl.link_people
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
python -m utils.afl.load_captains
python -m utils.afl.load_captains --report
```

Captain filters become available only after linked captaincy rows exist.

### Rising Star nominations

The Rising Star layer supports locally saved source pages and a permission-gated live fetch path:

```bash
python -m afl.fetch_footywire_rising_star --html-dir SAVED_PAGES --load-db
```

After written permission for live access:

```bash
python -m afl.fetch_footywire_rising_star --permission-confirmed --load-db
```

The importer can also be run separately:

```bash
python -m utils.afl.load_rising_star
```

### Broad family relationships

Wikipedia's broad football-family list can be scraped and conservatively
linked for sibling, parent-child and extended-family search:

```bash
python -m afl.scrape_wikipedia_families --report
python -m utils.afl.load_family_relationships --report --details
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
python -m utils.afl.fetch_club_sources --report
python -m utils.afl.load_club_sources --report --details
python tests/test_club_sources.py
```

AFL Tables automatic requests are permission-gated. The utility can print or
open the complete reviewed source manifest for browser-assisted saving, while
Wikipedia is fetched through the MediaWiki API. Run
`python -m utils.afl.fetch_club_sources --help` for the full workflow.

### Club all-games match sources

A separate pass reads each club's AFL Tables "all games" page and reconciles
the two clubs' accounts of the same match against each other and against the
derived `matches` table:

```bash
python -m utils.afl.fetch_historical_all_games
python -m utils.afl.load_club_all_games --report
```

This writes `club_match_sources`, `match_details` (quarter-by-quarter scores,
attendance, scheduled time) and `club_match_source_issues`, and backfills
attendance onto `matches` where both clubs agree.

**This layer has no user interface yet.** The data loads and is queryable
through the CLI and SQL, but no application page reads it. Nothing depends on
it, so it is safe to skip.

### Wikipedia reference scrape (NBA, NFL, MLB)

`wiki_sports_scraper.py` collects Wikipedia reference data for the three
American leagues: a catalogue of every current franchise, plus whatever the
team, league and Hall of Fame pages say under headings such as "Retired
numbers", "Franchise leaders" and "Hall of Fame". `utils/shared/load_wiki_reference.py`
validates that output and stages it into each sport's own database:

```bash
python -m utils.shared.load_wiki_reference --check
python -m utils.shared.load_wiki_reference
python -m utils.shared.load_wiki_reference --profile mlb
python tests/test_wiki_reference.py
```

The default source is the scraper's own output root, `C:\data_lab\wiki`;
pass `--dir` for another. Validation runs first and blocks the import on a
short team list, a failed team page, invalid `data_json` or a missing file
— the scraper exits cleanly whether or not its own log recorded problems,
so a completed run is not by itself a loadable one.

The records land in `wiki_reference_stage`, not in normalised tables. Their
structure lives in `data_json`, whose keys come from whatever columns the
Wikipedia table happened to have: this run produced 73 distinct key sets
across the NBA's table rows, 91 across the NFL's and 26 across the MLB's,
including positional keys where the page gave no usable header.
`--profile` reports those key sets, which is the inventory a later
normalisation pass would be designed from. `wiki_team_stage` holds the
franchise catalogue with each raw cell kept beside its parsed value, and
`wiki_team_map` records how each Wikipedia team name resolved to a
`clubs` row — four MLB franchises are `alias_match` rather than `matched`,
because Wikipedia names them as they are named now and the database was
built from sources that named them as they used to be.

Re-importing is idempotent. Each record carries a content hash that
excludes its position on the page, so a table an editor moved is recognised
as the same record rather than staged again.

The loader also fills `club_wikipedia_fields` — the Team Explorer's field
table — from the catalogue, but **only for keys a club has no value for
already**. The existing per-sport loaders read curated sources, and a
Wikipedia infobox cell is not a reason to overwrite one. Pass
`--no-fields` to stage the reference data and leave that table alone.

**This layer is reference material.** It does not carry games, scores,
schedules, player careers, rosters, drafts or stable external identifiers,
and nothing in it is written into `matches`, `players` or `player_seasons`.

### Family-draft relationships

Family-draft data is loaded and linked separately:

```bash
python -m utils.afl.load_family_draft
python -m utils.afl.load_family_draft --report --details
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
`python -m utils.shared.load_stat_coverage --sport nba` and read back through
`data/nba/reference/nba_reference.json`.

## Data sources

### Basketball-Reference

`nba/nba_source_bbr.py` reads a local scrape of Basketball-Reference: two index
CSVs and one JSON box score per game.

**This adapter is for a private, local prototype only.** Sports Reference
permits its data to be used for personal, non-commercial research. It does
not clear republishing a comprehensive copy of the site's data, or using it
to back a public service, without permission. A database built from the
scrape is fine to hold and query locally and is **not** cleared for
redistribution or for backing a hosted application — the same standing as
the NBA.com adapter below.

The scrape records a sha256 for every page it read; the adapter carries
those digests into `source_manifest`, so two disagreeing builds can be
traced to a page that changed rather than argued about.

Two things the adapter reconciles rather than trusts, both documented at
the top of the module: the scrape's `game_date` column currently repeats
the game key, so dates are parsed from the key's first eight digits; and a
Basketball-Reference team key is era-specific (`PHW`, `SFW` and `GSW` are
one franchise), so the key is treated as the historical identity and the
franchise comes from `nba_reference.club_lineage()`.

### NBA.com via nba_api

`nba/nba_source_api.py` reads NBA.com's statistics endpoints through the
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
is why `nba/build_nba_db.py` is written against `nba_source.NbaSource` rather
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

`afl/build_db.py` downloads the community-maintained cached AFL Tables player-stat dataset published by the fitzRoy project. A single cached dataset replaces a large number of individual page requests.

### Draftguru

Used by optional local importers for draft, recruitment and award history.

### FootyWire

Used for Rising Star nomination history. Live fetching is permission-gated. Manually saved pages may be parsed locally for personal use.

### Lahman baseball database

The MLB build reads Sean Lahman's baseball database, the long-running
community-maintained CSV export of MLB career, season and postseason
records. It is published for research use under a Creative Commons
Attribution-ShareAlike licence; the CSVs are not redistributed here. Put
your own copy under `data/mlb/raw/` and see
[`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) for the terms.

### nflverse

The NFL build downloads nflverse's released datasets through `nflreadpy`:
weekly player and team statistics, schedules, rosters, draft picks, the team
catalogue, and the eight optional `--extended` layers. nflverse publishes
under a Creative Commons Attribution 4.0 licence and the data originates with
the NFL and its statistics providers. Nothing is redistributed here -- the
build downloads it, and `data/` is gitignored. See
[`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) for the terms.

### Gridley

Gridley inspired the original grid-solving workflow. Sports Data Lab is an unaffiliated research and puzzle-support tool. It does not reproduce Gridley's live crowd selection percentages.

See [`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) for source credits, terms and reuse notes.

## Architecture and design patterns

**One app, multiple sports**: A shared query engine (`core.py`), database schema interface (`sports.py`), and Streamlit application serve all four sports. Sport-specific logic (constraints, awards, draft data) lives in each sport's package; pages check for capabilities rather than hardcoding sport names. This keeps the sport-agnostic framework clean and makes adding a sport straightforward.

**Incremental database updates**: MLB and NBA adopt a "build once, append current data" pattern. A full rebuild establishes complete history from a canonical source (Lahman CSV for MLB, a cached NBA.com snapshot for NBA); subsequent automated updates from the live source (Stats API for MLB, NBA.com for NBA) append/replace only the current season. This sidesteps source limitations (Lahman has no per-game data; NBA.com cannot represent historical team identity changes) and keeps updates fast. See [Automated database updates](#automated-database-updates).

**Stable match and player IDs**: Matches are identified by a stable `match_id` derived from (season, round, date, home_team, away_team); this lets refreshes reuse IDs rather than creating duplicates. Players are tracked by `player_id` (internal sequential) and optionally by `source_player_id` (from the upstream source), which is how incremental loaders match incoming rows to existing roster entries without name ambiguity.

**Conservative data linking**: Optional data layers (awards, captaincy, draft history) are resolved to `player_id` only when the link is trusted and unique. Ambiguous or unmatched records are retained for reporting and debugging but excluded from search and grid results.

**Query parameterisation**: User input is never concatenated into SQL. Every filter compiles to a parameterised query with bound values, which prevents injection and makes caching safe.

**Read-only application**: The Streamlit app opens its database in read-only mode. All mutations (builds, imports, updates) happen offline through command-line scripts, which are staged and promoted atomically (write to `.building`, verify, replace).

## Repository layout

Each sport owns a runtime package for constraints, builds, source adapters,
and sport-only pages. One-shot loaders and database maintenance live under
`utils/`, grouped into `afl/`, `nba/`, `nfl/`, `mlb/`, and `shared/`. The
repository root holds the multi-sport application framework.

```
app.py  core.py  sports.py  explore.py  ...   the sport-agnostic framework
afl/    constraints, build, scrapers, Club Explorer, Game Lab
nba/    constraints, build, source adapters, Basketball-Reference staging
mlb/    constraints, the Lahman build, franchise reference
nfl/    constraints, the nflverse build, franchise reference
data/   afl/  nba/  mlb/  nfl/  -- databases, raw sources, caches, references
utils/  afl/  nba/  nfl/  mlb/  shared/  operational tooling
tests/  docs/
```

The sport packages are imported, not path-executed, so their scripts run as
modules from the repository root:

```bash
python -m afl.build_db
python -m nba.build_nba_db --source csv
python -m mlb.build_mlb_db
python -m nfl.build_db --all-history
```

Two rules keep the split honest, and both are enforced by
`tests/test_sport_capabilities.py`: no module outside `sports.py`,
`data_paths.py` and `theme.py` may branch on a sport's key, and every
sport-only page is reached through a capability field on the `Sport`
object rather than an `if`.

**Key active files:**

| File | Purpose |
|---|---|
| `database_updates.py` | Automated pipeline: manages "regular" and "full" update events |
| `app.py` | Streamlit application, navigation, multi-sport picker |
| `core.py` | Sport-agnostic: schema, query compiler, obscurity ranking, intersection engine |
| `sports.py` | Sport registry; each sport declares its schema, constraints, pages, vocabulary |
| `advanced_search.py` | URL-addressable player search with a compact query language |
| `explore.py` | Core pages: Home, Player Search, Game Lab, Stats Explorer, Discovery |
| `query_filters.py` | Parser and SQL compiler for the search language |
| `obscurity.py` | Sport-agnostic, term-driven obscurity model for ranking |
| `afl/constraints.py` | AFL constraint builders (via re-export from captains, brownlow, rising_star, etc.) |
| `afl/build_db.py` | AFL full rebuild from fitzRoy cached dataset |
| `afl/game_lab.py` | AFL-specific game exploration modes |
| `nba/constraints_nba.py` | NBA constraint builders |
| `nba/build_nba_db.py` | NBA full rebuild (from CSV, BBR scrape, or NBA.com) |
| **`utils/nba/load_current_season.py`** | **NBA incremental loader: appends/refreshes current seasons from live source** |
| `nba/nba_source.py` | NBA source adapter contract; CSV and live-source adapters |
| `nba/nba_source_api.py` | NBA.com adapter via nba_api (private local use only) |
| `nba/nba_reference.py` | NBA team list, franchise lineage, measured stat eras |
| `mlb/constraints_mlb.py` | MLB constraint builders (row-scoped team/stat pairing for Immaculate Grid) |
| `mlb/build_mlb_db.py` | MLB full rebuild from Lahman CSV export |
| **`utils/mlb/load_statsapi.py`** | **MLB incremental loader: appends/refreshes seasons from Stats API** |
| `mlb/mlb_reference.py` | MLB franchise list, lineage, measured stat eras |
| `nfl/constraints_nfl.py` | NFL constraint builders |
| `nfl/build_db.py` | NFL builder (downloads nflverse data through nflreadpy) |
| `utils/nfl/patch_nfl_db.py` | NFL adapter: derives columns the app needs from nflverse tables |
| `afl/derive_matches.py` | Match derivation and stable match ID assignment (reusable for any sport) |
| `data_paths.py` | Single source of truth for all database and data paths |
| `theme.py` | Dark, Light, and Custom appearance modes |
| `health.py` | Database health page: schema, row counts, optional-data status |
| `afl/historic_grids.py` | Captured Gridley board validation and practice-mode support |
| `afl/parse_criteria.py` | Text parser for grid criteria (e.g., "50+ GAMES TWO DIFF CLUBS") |
| `afl/club_explorer.py` | Club metadata and records page |
| `ui_widgets.py` | Search widgets, player options, axis builders, label rendering |
| `branding.py` | Tab icon and iOS home-screen icon, read from `static/icons/` |
| `accounts_ui.py` | Account/session management UI |
| `tests/` | Regression tests, integration tests, live smoke tests |
| `ACKNOWLEDGEMENTS.md` | Data-source credits and reuse terms |

**Utilities and optional loaders** (sport-specific data layers):

| File | Purpose |
|---|---|
| `utils/afl/load_draftguru.py` | Parse Draftguru scrape for draft, recruitment, awards |
| `utils/afl/load_captains.py` | Link captaincy data |
| `utils/afl/fetch_footywire_rising_star.py` | Rising Star nomination scraper and loader |
| `utils/afl/load_family_relationships.py` | Wikipedia family-relationship parser and loader |
| `utils/afl/load_family_draft.py` | Family-draft relationship loader |
| `utils/afl/load_club_sources.py` | Club metadata and all-time records loader |
| `utils/afl/fetch_club_sources.py` | Cache club source pages |
| `utils/afl/load_round_csv.py` | Load a hand-entered AFL Tables round the source dataset has not published yet |
| `utils/afl/load_round_gui.py` | Browse window over that loader: pick the folder, check it, load it |
| `utils/shared/load_wiki_reference.py` | Wikipedia reference import (NBA/NFL/MLB) |
| `utils/shared/clean_project.py` | Identify and remove generated artefacts |
| `utils/shared/optimise_database.py` | Index optimization suggestions |
| `utils/nba/load_and_link_nba_sample.py` | NBA sample-data ingestion and identity resolution (`data/nba/sample`) |

**Diagnostics** (read-only; none of these write to a database):

| File | Purpose |
|---|---|
| `utils/search_cli.py` | Command-line front end to the Advanced Search compiler |
| `utils/shared/diagnose_answer.py` | Explain why the solver thinks a player answers a square, and log rejections |
| `utils/afl/audit_all_games_linkage.py` | How much of the all-games history reached the queryable tables |
| `utils/afl/audit_club_nicknames.py` | Club identity and nickname coverage |
| `utils/afl/validate_club_records.py` | Validate the cached AFL Tables record pages in `data/afl/raw/clubs` |

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

`utils/shared/clean_project.py` reviews generated artefacts and legacy files. It is a
dry run unless `--apply` is supplied.

```bash
python -m utils.shared.clean_project --root .
python -m utils.shared.clean_project --root . --apply
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

- **AFL is the most complete implementation.** It includes all application pages and optional data layers (draft, awards, captaincy, Rising Star, family relationships, club metadata). The other sports have all core pages (search, grid solver, game lab, stats) but lack sport-specific grid libraries and some optional extensions.
- **The NBA build is a private, local prototype.** It is not cleared for redistribution or for backing a hosted application. See [NBA.com via nba_api](#nbacom-via-nba_api) for licensing.
- **No teammate constraint for NBA.** Teammates cannot be reliably matched because trades can put players on the same team mid-season without a shared match. Answering "was a teammate" properly would require shared game lineups (`player_game` linking), which is not built. The constraint is absent rather than present and wrong.
- **NBA seasons are stored as start years:** `played:2005` means the 2005-06 season.
- **MLB row grain is player-season, not player-game.** Lahman's finest detail is a player's season total for one team; there are no per-game statistics. This prevents "X+ of a stat in one game" and per-game teammate squares. Multi-stat squares still work ("100+ RBI and 200+ hits in the same season" is valid).
- **NFL coverage is 1999 onward.** nflverse's weekly statistics begin in 1999; rosters and schedules reach further back but are not usable for game squares.
- **Obscurity models differ by sport.** Scores come from different formulas with different terms, which is why the model version is stored alongside every score.
- **Name matching is conservative.** Historical player records cannot be safely resolved by name alone; internal ID matching is preferred. Ambiguous unresolved records are retained for reporting but excluded from answers.
- **Some historical grids contain unsupported criteria.** The grid parser reports these rather than guessing. Practice mode can substitute a supported alternative with a clear label.
- **The obscurity rating is heuristic and not official.** It is a database proxy, not Gridley's live crowd percentage.
- **Historical statistics are limited by recording era.** Each statistic was recorded starting in a different season; empty historical values are NULL, not zero.
- **The project does not provide a hosted database or application.** It is a local-first research tool.

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
