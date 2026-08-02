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
`build_db.py` downloads the community-maintained cached copy of the AFL
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

**Gridley** — <https://gridleygame.com>
The daily AFL grid puzzle the solver was originally written for. This
project is an unaffiliated fan tool; it does not reproduce Gridley's puzzles
or its crowd-sourced rarity percentages (the obscurity score here is an
independent fame proxy, not Gridley's data).

## Libraries

- **pandas** and **NumPy** — all the data reshaping
- **pyreadr** — reading the R `.rda` dataset
- **Streamlit** — the entire UI
- **SQLite** — via the Python standard library

## A note on reuse

The code in this repository is ours to license. The data is not. If you fork
this, build your own database from the sources above and follow their terms.
AFL, club names and the Brownlow, Coleman and Norm Smith medals are
trademarks of their respective owners; this is an unaffiliated hobby project.