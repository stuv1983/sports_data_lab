"""Database path policy for the multi-sport repository layout.

Every module that needs "the AFL database" resolves it here, so the
modern-versus-legacy decision is written exactly once.
"""

from pathlib import Path

#: Legacy single-file databases that predate the data/<sport>/ layout.
LEGACY = {"afl": "gridley.db", "nba": "nba.db"}


def sport_db(sport_key: str, legacy: str | None = None) -> str:
    """Return ``data/<sport>/<sport>.db`` when present, else a legacy path."""
    key = sport_key.strip().lower()
    modern = Path("data") / key / f"{key}.db"
    if legacy is None:
        legacy = LEGACY.get(key)
    if modern.exists() or legacy is None:
        return str(modern)
    return str(Path(legacy))


def default_db(sport_key: str) -> str:
    """The canonical read/write database path for a sport."""
    return sport_db(sport_key)


def raw_dir(sport_key: str) -> Path:
    return Path("data") / sport_key.strip().lower() / "raw"


def cache_dir(sport_key: str, name: str | None = None) -> Path:
    """Scratch space for archived source pages.

    ``scrape_afl_captains.py`` stores fetched Wikipedia pages here so a rerun
    does not re-request them.  A named subfolder keeps one scraper's pages
    separate from another's::

        data/afl/cache/captain_pages/
    """
    base = Path("data") / sport_key.strip().lower() / "cache"
    return base / name if name else base


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


def rising_star_sources(sport_key: str = "afl") -> list[Path]:
    """Prefer the combined CSV, otherwise load the per-season files."""
    base = rising_star_dir(sport_key)
    combined = base / "rising_star_nominees.csv"
    if combined.exists():
        return [combined]
    return sorted((base / "csv").glob("rising_star_nominees_*.csv"))


def family_draft_sources(sport_key: str = "afl") -> list[Path]:
    """Canonical Wikipedia family-draft CSV, with legacy-name fallback."""
    base = raw_dir(sport_key)
    canonical = base / "wikipedia_family_draft.csv"
    if canonical.exists():
        return [canonical]
    fallback = base / "family_draft.csv"
    return [fallback] if fallback.exists() else []
