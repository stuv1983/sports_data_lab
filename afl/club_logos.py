"""Resolve club logo files to club_ids.

Logos live in ``resources/teams/afl``, one folder per sport alongside
``nba``, ``mlb`` and ``nfl``. Filenames come from wherever the
image came from and are not consistent -- ``Brisbane_Lions.svg`` next to
``GCSuns_2024.svg`` next to ``Collingwood_Football_Club_Logo_(2017-present)
.svg`` -- so this module matches them to clubs rather than requiring them
to be renamed.

Adding or replacing a logo
--------------------------

Drop the file in the folder. Matching is automatic if the filename
contains the club's name, nickname or abbreviation, which covers every
sensible name a downloaded logo arrives with. Nothing else to edit, and a
newer file for a club that already has one simply replaces it -- delete the
old file, or list the one you want in the override file below.

When automatic matching gets it wrong or a filename is too cryptic, add a
line to ``resources/teams/afl/logos.csv``::

    club_id,filename
    gold_coast,GCSuns_2024.svg

An override always wins, and only needs to name the files in dispute.
``unmatched()`` lists any file that resolved to no club, so a logo that
silently is not being displayed is visible rather than just absent.

Matching is longest-key-first and each file is claimed once, because the
short keys are substrings of the long ones: 'adelaide' appears inside
'PortAdelaideFootballClub_2019' and 'melbourne' inside
'North_Melbourne_FC_logo'. Matching those in the wrong order gives Port
Adelaide's logo to Adelaide.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from data_paths import ROOT

def logo_dir(sport_key: str = "afl") -> Path:
    """One folder per sport under resources/teams."""
    return ROOT / "resources" / "teams" / sport_key.strip().lower()


LOGO_DIR = logo_dir("afl")
OVERRIDE_FILE = "logos.csv"
SUFFIXES = (".svg", ".png", ".webp", ".jpg", ".jpeg", ".gif")

#: Extra words that identify a club in a filename, beyond its name and the
#: nickname the database already knows. Only needed where a file uses
#: neither -- 'GCSuns' has the nickname, so Gold Coast needs no entry here.
EXTRA_KEYS = {
    "gws": ("gws", "giants", "greaterwesternsydney"),
    "brisbane_lions": ("brisbanelions", "lions", "brisbane"),
    "western_bulldogs": ("westernbulldogs", "bulldogs", "footscray"),
    "north_melbourne": ("northmelbourne", "kangaroos"),
    "port_adelaide": ("portadelaide",),
    "west_coast": ("westcoast", "eagles"),
    "gold_coast": ("goldcoast", "suns", "gcsuns"),
    "st_kilda": ("stkilda", "saints"),
    "sydney": ("sydneyswans", "swans"),
    "adelaide": ("adelaidecrows", "crows"),
}


def _key(value: str) -> str:
    """Lower-case, letters and digits only, so spacing and punctuation
    stop mattering: 'St Kilda' and 'StKildaFC_2024' share a prefix."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def logo_files(folder: Path | None = None) -> list[Path]:
    folder = folder or LOGO_DIR
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.suffix.lower() in SUFFIXES and p.name != OVERRIDE_FILE)


def _overrides(folder: Path) -> dict[str, str]:
    path = folder / OVERRIDE_FILE
    if not path.exists():
        return {}
    out = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            club = (row.get("club_id") or "").strip()
            name = (row.get("filename") or "").strip()
            if club and name:
                out[club] = name
    return out


def _club_keys(clubs: dict[str, dict]) -> list[tuple[str, str]]:
    """(match_key, club_id) pairs, longest key first.

    `clubs` maps club_id -> {"name": ..., "nickname": ..., "abbrev": ...};
    every value is optional.
    """
    pairs: list[tuple[str, str]] = []
    for club_id, info in clubs.items():
        keys = {_key(club_id), _key(info.get("name")),
                _key(info.get("nickname"))}
        keys.update(_key(k) for k in EXTRA_KEYS.get(club_id, ()))
        abbrev = _key(info.get("abbrev"))
        # An abbreviation is two or three letters and matches far too much
        # on its own -- 'gc' appears in a dozen unrelated words. Only use
        # one when nothing longer is available for that club.
        if abbrev and len(abbrev) >= 3:
            keys.add(abbrev)
        for key in keys:
            if len(key) >= 3:
                pairs.append((key, club_id))
    pairs.sort(key=lambda pair: (-len(pair[0]), pair[1]))
    return pairs


def resolve(clubs: dict[str, dict],
            folder: Path | None = None) -> dict[str, Path]:
    """club_id -> logo path, for every club a file could be found for."""
    folder = folder or LOGO_DIR
    files = logo_files(folder)
    if not files:
        return {}

    by_name = {p.name: p for p in files}
    found: dict[str, Path] = {}
    claimed: set[Path] = set()

    for club_id, filename in _overrides(folder).items():
        path = by_name.get(filename)
        if path is not None:
            found[club_id] = path
            claimed.add(path)

    keyed = [(p, _key(p.stem)) for p in files]
    for key, club_id in _club_keys(clubs):
        if club_id in found:
            continue
        for path, filekey in keyed:
            if path in claimed:
                continue
            if key in filekey:
                found[club_id] = path
                claimed.add(path)
                break
    return found


def unmatched(clubs: dict[str, dict],
              folder: Path | None = None) -> list[Path]:
    """Logo files that resolved to no club — usually a naming surprise."""
    folder = folder or LOGO_DIR
    used = set(resolve(clubs, folder).values())
    return [p for p in logo_files(folder) if p not in used]


def missing(clubs: dict[str, dict],
            folder: Path | None = None) -> list[str]:
    """Clubs with no logo file."""
    found = resolve(clubs, folder)
    return [club_id for club_id in clubs if club_id not in found]


def clubs_from_db(con) -> dict[str, dict]:
    """Build the club map from `clubs` plus the scraped nickname."""
    have = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "clubs" not in have:
        return {}
    nicknames = {}
    if "club_wikipedia_fields" in have:
        from . import club_fields as CF
        for club_id, in con.execute("SELECT club_id FROM clubs"):
            fields = dict(con.execute(
                "SELECT field_key, field_value FROM club_wikipedia_fields "
                "WHERE club_id=?", (club_id,)))
            nicknames[club_id] = CF.headline(fields, "nickname").primary
    return {
        club_id: {"name": name, "abbrev": abbrev,
                  "nickname": nicknames.get(club_id, "")}
        for club_id, name, abbrev in con.execute(
            "SELECT club_id, name, abbreviation FROM clubs")
    }


def report(con, folder: Path | None = None) -> dict:
    """Everything a diagnostics page needs in one call."""
    clubs = clubs_from_db(con)
    found = resolve(clubs, folder)
    return {
        "folder": str(folder or LOGO_DIR),
        "files": len(logo_files(folder)),
        "clubs": len(clubs),
        "matched": {k: v.name for k, v in sorted(found.items())},
        "missing": missing(clubs, folder),
        "unmatched": [p.name for p in unmatched(clubs, folder)],
    }
