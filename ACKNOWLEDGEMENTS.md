# Acknowledgements

AFL Data Lab is a thin layer over other people's work. Nothing here would
exist without the sources below, and none of their data is redistributed in
this repository — the database is built locally from source, and
`gridley.db` is gitignored for that reason.

## Data

**AFL Tables** — <https://afltables.com>
The origin of essentially every player-game statistic in this project.
Decades of match-by-match records compiled and maintained by hand. This
project does **not** scrape afltables.com: `robots.txt` disallows automated
clients on the stats paths, and that is respected. The data reaches us via
the mirror below.

**fitzRoy and fitzRoy_data** — Jimmy Day and contributors
<https://github.com/jimmyday12/fitzRoy> · <https://github.com/jimmyday12/fitzRoy_data>
`afl/build_db.py` downloads the community-maintained cached copy of the AFL
Tables player-stats dataset from `fitzRoy_data`, the same file the R package
`fitzRoy` reads. One ~14 MB download replaces tens of thousands of page
requests. This is the single most load-bearing dependency in the project.

**Draftguru** — <https://www.draftguru.com.au>
Draft, trade, free agency, father-son, academy and award history. Fetched
one year-page at a time with a delay between requests, and cached to the
database so it runs once.

**FootyWire** — <https://www.footywire.com>
Rising Star nomination history. FootyWire's published terms prohibit
automated copying without prior written permission, so the live fetch path is
permission-gated. The importer can instead parse pages saved manually for
personal use. Raw HTML, CSVs and the derived database stay local and are not
redistributed.

**Wikipedia father-son and father-daughter lists** — <https://en.wikipedia.org/wiki/List_of_players_drafted_to_the_Australian_Football_League_under_the_father%E2%80%93son_rule>
AFL father-son and AFLW father-daughter draft relationships. Retrieved through
the MediaWiki API with the source revision and CC BY-SA 4.0 attribution stored
beside the local CSV. Raw source files and the derived database remain local.

**Wikipedia — List of Australian rules football families** —
<https://en.wikipedia.org/wiki/List_of_Australian_rules_football_families>
Family membership and explicitly stated sibling, parent-child and extended
family relationships. The scraper records the source URL and revision ID and
keeps ambiguous prose out of trusted query results. Wikipedia content is
available under CC BY-SA; attribution and revision metadata are retained.

**Wikipedia AFL club pages** — <https://en.wikipedia.org>
Current-club identity, names, nicknames, colours, grounds, roles, honours and
other infobox metadata. Retrieved through the MediaWiki API with source page,
revision ID, timestamp and raw snapshot retained locally. Wikipedia content is
available under CC BY-SA; attribution metadata is preserved.

**Wikipedia NBA, NFL and MLB league, team and Hall of Fame pages** —
<https://en.wikipedia.org>
Current-franchise catalogues for the three American leagues, taken from the
`National Basketball Association`, `National Football League` and `Major
League Baseball` pages, plus championships, retired numbers, franchise
leaders, honours and Hall of Fame membership from each team's own page and
from the three leagues' Hall of Fame list pages. Retrieved through the
MediaWiki API. Every staged record keeps its source page title, source URL,
revision ID and retrieval timestamp, and the raw API responses are archived
beside the output. Wikipedia content is available under CC BY-SA, subject to
the attribution terms on each page; attribution and revision metadata are
preserved on every row. Raw responses and the derived database remain local
and are not redistributed.

**AFL Tables club summaries** — <https://afltables.com>
Current-club player totals, all-time player lists and season/game record
leaderboards. Source HTML and derived database tables remain local and are not
redistributed. Automated fetching is disabled unless the operator explicitly
confirms permission; offline parsing of saved pages remains supported.

**Gridley** — <https://gridleygame.com>
The daily AFL grid puzzle the solver was originally written for. This
project is an unaffiliated fan tool; it does not reproduce Gridley's puzzles
or its crowd-sourced rarity percentages (the obscurity score here is an
independent fame proxy, not Gridley's data).

**Basketball-Reference** — <https://www.basketball-reference.com>
The NBA player-game, schedule and biographical data behind the local NBA
build. Sports Reference permits its data to be used for personal,
non-commercial research; it does not clear republishing a comprehensive copy
of the site's data or using it to back a public service without permission.
The NBA build in this repository is therefore a **private local prototype**:
the database stays on the machine that built it, and neither it nor an
application backed by it is redistributed or hosted. Every page read is
digested and recorded in the `source_manifest` table so what was taken, and
when, is auditable.

**NBA.com** — <https://www.nba.com/stats/>
NBA statistics, read through the community `nba_api` package. NBA.com's terms
permit statistics to be used for private, non-commercial purposes and require
attribution to NBA.com, but they do not clear offering a comprehensive,
regularly updated NBA statistics database through a website or service without
prior consent. The NBA build in this repository is therefore a **private local
prototype**: the database stays on the machine that built it, and neither the
database nor an application backed by it is redistributed or hosted. Every
retrieval is cached and recorded in the `source_manifest` table so what was
taken, and when, is auditable. `nba_api` is an unofficial community package;
NBA.com does not document these endpoints and does not announce changes to
them.

**Lahman baseball database** — <https://sabr.org/lahman-database/>
Sean Lahman's baseball database, maintained with SABR, is the source for the
entire MLB build: career and season batting, pitching and appearance records,
their postseason counterparts, franchise history, awards and Hall of Fame
voting, from 1871. It is published for research and non-commercial use under
a Creative Commons Attribution-ShareAlike licence, which asks for attribution
and that derived work carry the same terms. The CSV export is not
redistributed in this repository -- `mlb/build_mlb_db.py` reads a copy you
supply under `data/mlb/raw/`. Lahman has no box scores, so the MLB database
here is season-grain and says so; see the README.

**nflverse** — <https://github.com/nflverse>, via **nflreadpy**
The source for the entire NFL build: weekly player and team statistics,
schedules, rosters, draft picks and the team catalogue. nflverse publishes its
releases under a Creative Commons Attribution 4.0 licence, which asks for
attribution; the data itself originates with the NFL and its statistics
providers. Nothing is redistributed in this repository -- `nfl/build_db.py`
downloads it, and the raw releases are gitignored. The weekly player
statistics begin in 1999 even though the schedules and rosters reach back to
1920, so every career total in the NFL database is a 1999-onward figure and
the app says so rather than implying whole careers.

**Immaculate Grid** — <https://www.immaculategrid.com>
The daily grid puzzle format the NBA solver answers questions in. This project
is an unaffiliated fan tool; it does not reproduce Immaculate Grid's puzzles or
its crowd-sourced rarity percentages.

## Libraries

- **pandas** and **NumPy** — all the data reshaping
- **pyreadr** — reading the R `.rda` dataset
- **nba_api** — optional; the NBA.com adapter, see the note above
- **nflreadpy** — the nflverse client the NFL build downloads through
- **Streamlit** — the entire UI
- **SQLite** — via the Python standard library

## A note on reuse

The code in this repository is ours to license. The data is not. If you fork
this, build your own database from the sources above and follow their terms.
AFL, club names and the Brownlow, Coleman and Norm Smith medals are
trademarks of their respective owners, as are the NBA and the NFL, their team
names and their awards; this is an unaffiliated hobby project.