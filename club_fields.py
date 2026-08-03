"""Presentation helpers for scraped club metadata.

The Wikipedia infobox scrape stores each field exactly as the page renders
it, which is right for provenance and wrong for a headline. A club's
nickname arrives as ``'AFL: Demons, Dees Indigenous rounds: Narrm'`` and its
founding date as ``'1885 ; 141 years ago ( 1885 )'``; dropped into a metric
tile both truncate to something unreadable.

These functions turn one raw value into a short primary form plus the
detail that was factored out of it. They never discard anything silently:
the caller gets the extras back, and the Overview table still shows the
original string, so the headline is a summary of the source rather than a
replacement for it.

Pure string handling, no database and no Streamlit, so the rules are
testable on their own. Cleaning is best-effort by design -- an infobox is
not a schema, and a value these rules cannot improve is returned unchanged
rather than mangled into a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The keys the nickname actually lives under. The scrape derives the key
#: from the infobox row label, and Wikipedia writes that label three
#: different ways -- 'Nickname', 'Nicknames' and 'Nickname(s)', the last of
#: which normalises to `nickname_s`. Fourteen of the eighteen clubs use
#: `nickname_s`, so a lookup that checks only the first two finds nothing
#: for Adelaide, Collingwood, Fremantle, Gold Coast, Greater Western
#: Sydney, Hawthorn, Melbourne, North Melbourne, Port Adelaide, Richmond,
#: St Kilda, Sydney, West Coast and Western Bulldogs.
NICKNAME_KEYS = ("nickname_s", "nickname", "nicknames")
FOUNDED_KEYS = ("founded", "established")
GROUND_KEYS = ("ground", "grounds")
PREMIERSHIP_KEYS = ("premierships",)

#: Wikipedia reference and note markers: '[ 2 ]', '[ a ]', '[ citation ]'.
_FOOTNOTE = re.compile(r"\[\s*[^\[\]]{1,12}\s*\]")

#: An infobox sub-label: 'AFL:', 'AFLW/VFL:', 'Indigenous rounds:'.
#:
#: Curated rather than general. A pattern like ``[A-Za-z ]{0,24}:`` looks
#: more flexible but reads 'Crows Indigenous rounds:' as a single label,
#: leaving nothing before it and making the Indigenous-round name the
#: club's primary nickname -- Adelaide came out as 'Kuwarna' and West Coast
#: as 'Waalitj Marawar'. It also matches 'capacity:' inside a venue's
#: bracketed capacity and truncates the ground name there. An infobox uses
#: a small, stable set of qualifiers, so listing them is both safer and
#: more honest about what is being recognised.
#: A competition may be qualified by several others at once, which the
#: source writes with slashes or ampersands: 'AFLW/WAFL:', 'AFLW & VFL &
#: VFLW:'. Matching one token only would leave 'AFLW/' as a fragment.
_COMP_TOKEN = (r"(?:AFLW|AFL|VFLW|VFL|VFA|SANFLW|SANFL|WAFLW|WAFL|NEAFL)")
_COMP_LABEL = rf"{_COMP_TOKEN}(?:\s*[/&]\s*{_COMP_TOKEN})*"
_CONTEXT_LABEL = (r"Indigenous rounds?|Seniors|Reserves|Women's|"
                  r"Pre-?season|Night series|Home|Away|Training")
_LABEL = re.compile(rf"\b({_COMP_LABEL}|{_CONTEXT_LABEL}):\s*", re.I)

#: A venue name may trail the years it was used: 'Perth Stadium
#: 2018-present'. That is provenance, not part of the name.
_VENUE_YEARS = re.compile(
    r"\s*\b(?:1[89]\d{2}|20\d{2})\s*[-–—]\s*(?:present|\d{2,4})\b", re.I)

#: Competition prefixes that qualify a value rather than being part of it,
#: for the case where the source writes 'AFL Power, Port' with no colon.
_COMPETITION = re.compile(
    r"^(?:AFL|AFLW|VFL|VFA|SANFL|WAFL|NEAFL|VFL/AFL)\b[:\s]*", re.I)

#: Labels whose text is the club's primary identity rather than a variant
#: used in a specific context.
_PRIMARY_LABELS = {"", "afl", "vfl/afl", "vfl"}


@dataclass(frozen=True)
class Cleaned:
    """A short headline value plus whatever was factored out of it."""
    primary: str
    extras: list[str] = field(default_factory=list)
    raw: str = ""
    qualifier: str = ""      # e.g. 'VFL/AFL' for a premiership count

    def __bool__(self) -> bool:
        return bool(self.primary)

    @property
    def detail(self) -> str:
        """The extras as one line, for a caption under the headline."""
        return ", ".join(self.extras)


EMPTY = Cleaned("")


def strip_footnotes(value: str) -> str:
    """Remove '[ 2 ]'-style markers and collapse the whitespace they leave."""
    return re.sub(r"\s+", " ", _FOOTNOTE.sub(" ", str(value or ""))).strip()


def segments(value: str) -> list[tuple[str, str]]:
    """Split a labelled infobox value into (label, text) pairs.

    ``'AFL: Demons, Dees Indigenous rounds: Narrm'`` becomes
    ``[('AFL', 'Demons, Dees'), ('Indigenous rounds', 'Narrm')]``. Text
    before any label comes back with an empty label, which is the common
    case -- ``'Crows Indigenous rounds: Kuwarna'`` yields
    ``[('', 'Crows'), ('Indigenous rounds', 'Kuwarna')]``.
    """
    text = strip_footnotes(value)
    if not text:
        return []
    marks = list(_LABEL.finditer(text))
    if not marks:
        return [("", text)]

    out: list[tuple[str, str]] = []
    lead = text[:marks[0].start()].strip(" ,;")
    if lead:
        out.append(("", lead))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip(" ,;")
        if body:
            out.append((mark.group(1).strip(), body))
    return out


def _primary_segment(value: str) -> str:
    """The segment that carries the club's ordinary, everyday value."""
    parts = segments(value)
    if not parts:
        return ""
    for label, text in parts:
        if label.strip().lower() in _PRIMARY_LABELS:
            return text
    return parts[0][1]


def _split_list(text: str) -> tuple[list[str], bool]:
    """Split a nickname or venue list on its separators.

    Returns the items and whether the split had to fall back to
    whitespace. Wikipedia sometimes runs nicknames together with no
    punctuation at all ('Blues Blue Baggers Baggers Old Navy Blues'), where
    only the first word is reliably a name on its own -- the rest are
    fragments of multi-word nicknames. The flag lets the caller take the
    headline and decline to invent a list from the remainder.
    """
    items = [part.strip(" ,;&") for part in re.split(r"[,/;]|\s+&\s+", text)]
    items = [item for item in items if item]
    if len(items) == 1 and len(items[0].split()) > 2:
        return items[0].split(), True
    return items, False


def nickname(value: str) -> Cleaned:
    """Primary nickname, with the remaining ones as extras.

    'Tigers , Tiges'                      -> 'Tigers'   + ['Tiges']
    'AFL: Demons, Dees Indigenous...'     -> 'Demons'   + ['Dees']
    'Crows Indigenous rounds: Kuwarna'    -> 'Crows'    + ['Kuwarna']
    'Blues Blue Baggers Baggers Old...'   -> 'Blues'    + [...]

    The Indigenous-round and second-competition names are kept as extras
    rather than dropped; they are real names for the club, just not the one
    a headline wants.
    """
    raw = strip_footnotes(value)
    if not raw:
        return EMPTY
    primary_text = _COMPETITION.sub("", _primary_segment(raw)).strip()
    items, ran_together = _split_list(primary_text)
    if not items:
        return Cleaned(raw, [], raw)

    # With no punctuation to go on, only the first word is reliably a
    # nickname; the rest are pieces of multi-word ones. Show the headline
    # and let the Overview table carry the source string verbatim.
    extras = [] if ran_together else items[1:]
    for label, text in segments(raw):
        if label.strip().lower() in _PRIMARY_LABELS:
            continue
        more, fallback = _split_list(_COMPETITION.sub("", text).strip())
        if not fallback:
            extras.extend(more)
    seen, unique = {items[0].lower()}, []
    for extra in extras:
        if extra.lower() not in seen:
            seen.add(extra.lower())
            unique.append(extra)
    return Cleaned(items[0], unique, raw)


def founded(value: str) -> Cleaned:
    """Founding year, with the full source date as the extra.

    '1885 ; 141 years ago ( 1885 )'          -> '1885'
    '18 July 1859 ; 167 years ago ( ... )'   -> '1859' + ['18 July 1859']

    The 'N years ago' clause is Wikipedia's rendering of a template and
    goes stale the moment it is stored, so it is never shown.
    """
    raw = strip_footnotes(value)
    if not raw:
        return EMPTY
    # Everything before the ';' is the date; the rest is the stale clause.
    head = raw.split(";")[0].strip()
    head = re.sub(r"\(.*?\)", "", head).strip(" ,")
    year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", head or raw)
    if not year:
        return Cleaned(head or raw, [], raw)
    extras = [head] if head and head != year.group(1) else []
    return Cleaned(year.group(1), extras, raw)


def ground(value: str) -> Cleaned:
    """Primary home ground, with the other listed grounds as extras.

    'AFL: Melbourne Cricket Ground (100,024) Ninja Stadium (20,000)
     AFLW/VFL: Punt Road Oval (2,800)'  ->  'Melbourne Cricket Ground'

    Capacities are dropped from the headline because they are what pushes
    the value past the width of a tile, and they are still in the Overview
    table.
    """
    raw = strip_footnotes(value)
    if not raw:
        return EMPTY

    def venues(text: str) -> list[str]:
        # A capacity in brackets terminates a venue name, so the bracketed
        # groups are the separators rather than something to strip in place.
        pieces = re.split(r"\([^)]*\)", _VENUE_YEARS.sub("", text))
        return [p.strip(" ,;&") for p in pieces if p.strip(" ,;&")]

    primary_list = venues(_COMPETITION.sub("", _primary_segment(raw)))
    if not primary_list:
        return Cleaned(raw, [], raw)

    extras = primary_list[1:]
    for label, text in segments(raw):
        if label.strip().lower() in _PRIMARY_LABELS:
            continue
        extras.extend(venues(text))
    seen, unique = {primary_list[0].lower()}, []
    for extra in extras:
        if extra.lower() not in seen:
            seen.add(extra.lower())
            unique.append(extra)
    return Cleaned(primary_list[0], unique, raw)


def premierships(value: str) -> Cleaned:
    """Senior premiership count, with the other competitions as extras.

    'VFL/AFL (13) 1920 1921 ...'  -> primary '13', qualifier 'VFL/AFL'
    'AFL (0) NEAFL (1) 2016'      -> primary '0',  qualifier 'AFL'

    Only the count is shown. The winning years are a list of up to twenty
    four-digit numbers and belong in the Overview table, not a tile.
    """
    raw = strip_footnotes(value)
    if not raw:
        return EMPTY
    counts = re.findall(r"([A-Za-z][A-Za-z/ ]*?)\s*\((\d+)\)", raw)
    if not counts:
        return Cleaned(raw, [], raw)
    label, count = counts[0]
    extras = [f"{other.strip()} {n}" for other, n in counts[1:]]
    return Cleaned(count, extras, raw, qualifier=label.strip())


#: field group -> (keys to try, cleaning function)
HEADLINES = {
    "nickname": (NICKNAME_KEYS, nickname),
    "founded": (FOUNDED_KEYS, founded),
    "ground": (GROUND_KEYS, ground),
    "premierships": (PREMIERSHIP_KEYS, premierships),
}


def headline(fields: dict[str, str], group: str) -> Cleaned:
    """Look a field group up across its known key spellings and clean it."""
    keys, clean = HEADLINES[group]
    for key in keys:
        value = fields.get(key)
        if value and str(value).strip():
            return clean(value)
    return EMPTY
