"""Database path policy for the multi-sport repository layout.

Every module that needs "the AFL database" resolves it here, so the
modern-versus-legacy decision is written exactly once.
"""

from pathlib import Path

#: The repository root. Every path below is anchored here rather than to the
#: process working directory, so `streamlit run C:\sports_data_lab\app.py`
#: finds the same database whichever folder it was launched from.
ROOT = Path(__file__).resolve().parent

#: Legacy single-file databases that predate the data/<sport>/ layout.
#: Only for a file that genuinely used to live at the repository root:
#: listing a sport here makes sport_db() resolve outside data/<sport>/ for
#: exactly as long as the modern database is missing.
LEGACY = {"afl": "gridley.db"}


def sport_db(sport_key: str, legacy: str | None = None) -> str:
    """Return ``data/<sport>/<sport>.db`` when present, else a legacy path."""
    key = sport_key.strip().lower()
    modern = ROOT / "data" / key / f"{key}.db"
    if legacy is None:
        legacy = LEGACY.get(key)
    if modern.exists() or legacy is None:
        return str(modern)
    return str(ROOT / legacy)


def default_db(sport_key: str) -> str:
    """The canonical read/write database path for a sport."""
    return sport_db(sport_key)


def staging_db(sport_key: str, name: str) -> Path:
    """A scratch database for an import that is not the app's database.

    Staging output belongs beside the real database, never in it: a file at
    ``sport_db(...)`` with a `players` table of the wrong shape makes
    ``Sport.exists()`` say yes and every query then fail on a missing column.
    """
    base = ROOT / "data" / sport_key.strip().lower() / "staging"
    base.mkdir(parents=True, exist_ok=True)
    return base / name


def raw_dir(sport_key: str) -> Path:
    return ROOT / "data" / sport_key.strip().lower() / "raw"


def cache_dir(sport_key: str, name: str | None = None) -> Path:
    """Scratch space for archived source pages.

    ``afl/scrape_afl_captains.py`` stores fetched Wikipedia pages here so a rerun
    does not re-request them.  A named subfolder keeps one scraper's pages
    separate from another's::

        data/afl/cache/captain_pages/
    """
    base = ROOT / "data" / sport_key.strip().lower() / "cache"
    return base / name if name else base


def nba_scrape_root() -> Path:
    """Where the Basketball-Reference scrape writes its data.

    The scrape runs outside the repository -- it is tens of gigabytes of
    JSON and must not land in git -- so the location is configuration, not
    layout. ``NBA_SCRAPE_ROOT`` wins; otherwise ``data/nba/raw/bbr``, which
    is where a symlink or a copied export belongs.

    ``nba_source_bbr.BbrNbaSource`` resolves through here, and
    ``nba/build_nba_db.py --source-root`` overrides it for one run.
    """
    import os
    configured = os.environ.get("NBA_SCRAPE_ROOT", "").strip()
    if configured:
        return Path(configured)
    return raw_dir("nba") / "bbr"


def reference_dir(sport_key: str) -> Path:
    """Checked-in and build-generated reference data for a sport.

    Distinct from raw/ and cache/: these files are *outputs* of a build that
    later become *inputs* to importing the app. nba_reference.json is the
    working example -- the NBA team list and measured stat eras are
    discovered by nba/build_nba_db.py and then read back by sports.py at import
    time, because core.Schema is a frozen dataclass built before any
    database is open.
    """
    return ROOT / "data" / sport_key.strip().lower() / "reference"


def captaincy_sources(sport_key: str = "afl") -> list[Path]:
    """CSV files that feed the optional captaincy layer, in load order.

    Both a single canonical file and a directory of per-era files are
    supported so the layer can grow without new configuration:

        data/afl/raw/captaincies.csv
        data/afl/raw/captains/*.csv
    """
    base = raw_dir(sport_key)
    sources: list[Path] = []
    for name in ("captaincies.csv", "wikipedia_captaincies.csv"):
        single = base / name
        if single.exists():
            sources.append(single)
            break
    folder = base / "captains"
    if folder.is_dir():
        sources.extend(sorted(folder.glob("*.csv")))
    return sources

def rising_star_dir(sport_key: str = "afl") -> Path:
    """Local FootyWire Rising Star cache and CSV directory."""
    return raw_dir(sport_key) / "footywire" / "rising_star"


def rising_star_wikipedia_dir(sport_key: str = "afl") -> Path:
    """Weekly Wikipedia Rising Star nominations, per season.

    Separate from the FootyWire cache above because they are separate
    sources with separate terms, not two copies of one thing. FootyWire may
    only be fetched by hand; Wikipedia is refreshed on a Monday timer.
    """
    return raw_dir(sport_key) / "wikipedia" / "rising_star"


def rising_star_sources(sport_key: str = "afl") -> list[Path]:
    """Rising Star CSVs in load order, FootyWire first then Wikipedia.

    Order is not preference -- ``utils/afl/load_rising_star.py`` decides
    that per round from each row's ``source`` column, so a FootyWire round
    wins wherever it exists regardless of which file was read first.
    """
    base = rising_star_dir(sport_key)
    combined = base / "rising_star_nominees.csv"
    sources = ([combined] if combined.exists()
               else sorted((base / "csv").glob("rising_star_nominees_*.csv")))
    wikipedia = rising_star_wikipedia_dir(sport_key) / "csv"
    sources.extend(sorted(wikipedia.glob("rising_star_nominees_*.csv")))
    # Hand-entered nominations, suspensions and vote counts, written by the
    # Admin page. Last so a reader sees the order edits are applied in;
    # precedence is still decided per round from the `source` column.
    manual = reference_dir(sport_key) / "rising_star_manual.csv"
    if manual.exists():
        sources.append(manual)
    return sources


def brownlow_sources(sport_key: str = "afl") -> list[Path]:
    """Yearly AFL Tables Brownlow result CSVs, excluding summary exports."""
    base = ROOT / "data" / sport_key.strip().lower() / "bm"
    return sorted(
        (path for path in base.glob("[0-9][0-9][0-9][0-9].csv")
         if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )


def family_draft_sources(sport_key: str = "afl") -> list[Path]:
    """Canonical Wikipedia family-draft CSV, with legacy-name fallback."""
    base = raw_dir(sport_key)
    canonical = base / "wikipedia_family_draft.csv"
    if canonical.exists():
        return [canonical]
    fallback = base / "family_draft.csv"
    return [fallback] if fallback.exists() else []


def family_relationship_dir(sport_key: str = "afl") -> Path:
    """Local Wikipedia broad-family CSV directory."""
    return raw_dir(sport_key)


def family_member_sources(sport_key: str = "afl") -> list[Path]:
    """CSV rows for every person in a listed football family."""
    path = family_relationship_dir(sport_key) / "wikipedia_family_members.csv"
    return [path] if path.exists() else []


def family_relationship_sources(sport_key: str = "afl") -> list[Path]:
    """CSV rows for explicit sibling/parent/extended relationships."""
    path = family_relationship_dir(sport_key) / "wikipedia_family_relationships.csv"
    return [path] if path.exists() else []
