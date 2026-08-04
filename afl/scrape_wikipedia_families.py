#!/usr/bin/env python3
"""Scrape Wikipedia's list of Australian rules football families.

The source is a long collection of family sections rather than a table.  This
scraper therefore produces two auditable datasets:

* ``wikipedia_family_members.csv`` -- every listed person and their family;
* ``wikipedia_family_relationships.csv`` -- only relationships supported by
  an explicit list label (``Son:``, ``Cousin:``, etc.) or a conservative prose
  rule (``A and B were brothers``, ``A is the father of B``).

People in the same section are known to belong to the same listed family, but
are not automatically assigned a direct relationship.  That distinction keeps
large families useful without inventing cousin/sibling/parent links.

The script makes one MediaWiki API request and retains the response in a local
cache.  Use ``--offline`` to rebuild CSVs from the cache without another
request.  If a live request fails, the cache is used automatically.

Dependencies:
    pip install requests beautifulsoup4

Examples:
    python scrape_wikipedia_families.py
    python scrape_wikipedia_families.py --refresh
    python scrape_wikipedia_families.py --offline
    python scrape_wikipedia_families.py --report
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin

API_URL = "https://en.wikipedia.org/w/api.php"
ARTICLE_URL = "https://en.wikipedia.org/wiki/List_of_Australian_rules_football_families"
PAGE_TITLE = "List of Australian rules football families"
USER_AGENT = (
    "SportsDataLab-family-relationships-scraper/1.0 "
    "(personal research; one Wikipedia page per run)"
)

MEMBER_COLUMNS = [
    "source_member_id",
    "family_key",
    "family_name",
    "member_name",
    "member_wikipedia_url",
    "clubs_raw",
    "list_depth",
    "member_order",
    "parent_source_member_id",
    "explicit_relation_label",
    "family_notes",
    "source_url",
    "source_revision_id",
    "scraped_at_utc",
]

RELATIONSHIP_COLUMNS = [
    "source_relationship_id",
    "family_key",
    "family_name",
    "person_a_source_member_id",
    "person_a_name",
    "person_a_role",
    "person_b_source_member_id",
    "person_b_name",
    "person_b_role",
    "relationship_type",
    "relationship_label",
    "evidence",
    "extraction_method",
    "confidence",
    "source_url",
    "source_revision_id",
    "scraped_at_utc",
]

# Canonical type and directional roles from a list label attached to person B,
# whose containing list item is person A.
LIST_RELATIONS: dict[str, tuple[str, str, str, str]] = {
    "son": ("parent_child", "parent", "son", "father-son/parent-son"),
    "daughter": ("parent_child", "parent", "daughter", "parent-daughter"),
    "child": ("parent_child", "parent", "child", "parent-child"),
    "father": ("parent_child", "child", "father", "father-child"),
    "mother": ("parent_child", "child", "mother", "mother-child"),
    "parent": ("parent_child", "child", "parent", "parent-child"),
    "grandson": (
        "grandparent_grandchild", "grandparent", "grandson", "grandparent-grandson"
    ),
    "granddaughter": (
        "grandparent_grandchild", "grandparent", "granddaughter",
        "grandparent-granddaughter",
    ),
    "grandchild": (
        "grandparent_grandchild", "grandparent", "grandchild",
        "grandparent-grandchild",
    ),
    "grandfather": (
        "grandparent_grandchild", "grandchild", "grandfather",
        "grandfather-grandchild",
    ),
    "grandmother": (
        "grandparent_grandchild", "grandchild", "grandmother",
        "grandmother-grandchild",
    ),
    "brother": ("sibling", "sibling", "brother", "siblings/brothers"),
    "sister": ("sibling", "sibling", "sister", "siblings"),
    "sibling": ("sibling", "sibling", "sibling", "siblings"),
    "twin": ("sibling", "twin", "twin", "twins"),
    "nephew": (
        "aunt_uncle_niece_nephew", "aunt_or_uncle", "nephew", "uncle/aunt-nephew"
    ),
    "niece": (
        "aunt_uncle_niece_nephew", "aunt_or_uncle", "niece", "uncle/aunt-niece"
    ),
    "uncle": (
        "aunt_uncle_niece_nephew", "niece_or_nephew", "uncle", "uncle/aunt-niece/nephew"
    ),
    "aunt": (
        "aunt_uncle_niece_nephew", "niece_or_nephew", "aunt", "uncle/aunt-niece/nephew"
    ),
    "cousin": ("cousin", "cousin", "cousin", "cousins"),
    "cousin once removed": (
        "cousin", "cousin_once_removed", "cousin_once_removed",
        "cousins once removed",
    ),
    "second cousin": ("cousin", "second_cousin", "second_cousin", "second cousins"),
    "husband": ("spouse", "spouse", "husband", "spouses"),
    "wife": ("spouse", "spouse", "wife", "spouses"),
    "spouse": ("spouse", "spouse", "spouse", "spouses"),
    "partner": ("spouse", "partner", "partner", "partners"),
    "son in law": ("in_law", "parent_in_law", "son_in_law", "in-laws"),
    "daughter in law": (
        "in_law", "parent_in_law", "daughter_in_law", "in-laws"
    ),
    "father in law": ("in_law", "child_in_law", "father_in_law", "in-laws"),
    "mother in law": ("in_law", "child_in_law", "mother_in_law", "in-laws"),
    "brother in law": ("in_law", "sibling_in_law", "brother_in_law", "in-laws"),
    "sister in law": ("in_law", "sibling_in_law", "sister_in_law", "in-laws"),
}

RELATION_LABEL_RE = re.compile(
    r"^(?P<label>"
    + "|".join(
        sorted((re.escape(label) for label in LIST_RELATIONS), key=len, reverse=True)
    )
    + r")\s*:\s*(?P<rest>.+)$",
    re.I,
)

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
TOKEN_RE = re.compile(r"§(\d+)§")


class ScrapeError(RuntimeError):
    """Raised when the source no longer matches the expected data shape."""


@dataclass
class Member:
    source_member_id: str
    family_key: str
    family_name: str
    member_name: str
    member_wikipedia_url: str
    clubs_raw: str
    list_depth: int
    member_order: int
    parent_source_member_id: str
    explicit_relation_label: str
    family_notes: str
    source_url: str
    source_revision_id: int | str
    scraped_at_utc: str

    def asdict(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in MEMBER_COLUMNS}


@dataclass
class Relationship:
    source_relationship_id: str
    family_key: str
    family_name: str
    person_a_source_member_id: str
    person_a_name: str
    person_a_role: str
    person_b_source_member_id: str
    person_b_name: str
    person_b_role: str
    relationship_type: str
    relationship_label: str
    evidence: str
    extraction_method: str
    confidence: str
    source_url: str
    source_revision_id: int | str
    scraped_at_utc: str

    def asdict(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in RELATIONSHIP_COLUMNS}


def _default_raw_dir() -> Path:
    try:
        from data_paths import raw_dir  # type: ignore
    except (ImportError, AttributeError):
        return Path("data") / "afl" / "raw"
    return Path(raw_dir("afl"))


def _default_cache_dir() -> Path:
    try:
        from data_paths import cache_dir  # type: ignore
    except (ImportError, AttributeError):
        return Path("data") / "afl" / "cache" / "wikipedia_families"
    return Path(cache_dir("afl", "wikipedia_families"))


def _normalise_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _ascii(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("’", "'").replace("`", "'")


def _key(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _ascii(value).casefold()))


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _key(value)).strip("-")


def _sha(*parts: object, width: int = 24) -> str:
    payload = "|".join(str(part or "") for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:width]


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temp = Path(handle.name)
        handle.write(content)
        handle.flush()
    temp.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def fetch_api_payload(
    cache_path: Path,
    *,
    offline: bool,
    timeout: float,
    no_cache: bool,
) -> tuple[dict[str, Any], str]:
    """Return MediaWiki API JSON and a source label."""
    if offline:
        if not cache_path.exists():
            raise ScrapeError(f"Offline mode requested but cache is missing: {cache_path}")
        return json.loads(cache_path.read_text(encoding="utf-8")), "cache"

    try:
        import requests
    except ImportError as exc:
        raise ScrapeError(
            "Missing dependency: requests. Run: pip install requests beautifulsoup4"
        ) from exc

    params = {
        "action": "parse",
        "page": PAGE_TITLE,
        "prop": "text|revid|displaytitle",
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
    }
    try:
        response = requests.get(
            API_URL,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ScrapeError(f"MediaWiki API error: {payload['error']}")
        if not isinstance(payload.get("parse", {}).get("text"), str):
            raise ScrapeError("MediaWiki API response is missing parse.text")
    except (requests.RequestException, ValueError, ScrapeError) as exc:
        if cache_path.exists():
            print(
                f"warning: live fetch failed ({exc}); using cached response",
                file=sys.stderr,
            )
            return json.loads(cache_path.read_text(encoding="utf-8")), "cache-fallback"
        if isinstance(exc, ScrapeError):
            raise
        raise ScrapeError(f"Could not fetch Wikipedia: {exc}") from exc

    if not no_cache:
        _atomic_write_json(cache_path, payload)
    return payload, "live"


def _clean_fragment(tag: Any, *, remove_nested_lists: bool = False) -> Any:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScrapeError(
            "Missing dependency: beautifulsoup4. Run: pip install beautifulsoup4"
        ) from exc

    clone = BeautifulSoup(str(tag), "html.parser")
    for node in clone.select(
        "sup.reference, span.reference, .mw-editsection, style, script, noscript"
    ):
        node.decompose()
    if remove_nested_lists:
        for node in clone.select("ul, ol"):
            node.decompose()
    return clone


def _visible_text(tag: Any, *, remove_nested_lists: bool = False) -> str:
    clone = _clean_fragment(tag, remove_nested_lists=remove_nested_lists)
    text = _normalise_space(clone.get_text(" ", strip=True))
    text = re.sub(r"\[(?:\d+|[a-z]|note\s*\d+)\]", "", text, flags=re.I)
    return _normalise_space(text)


def _first_wiki_url(tag: Any, expected_name: str) -> str:
    """Return the member article URL, never a club link.

    Some Wikipedia list items contain an unlinked player name followed by a
    linked club, for example ``Andrew Aish (Norwood)``.  Returning the first
    link in the item therefore treated the club page as the player's identity.
    Two unlinked relatives at the same club then received the same
    ``source_member_id`` and the live scrape failed validation.

    Match the visible anchor text to the parsed member name.  An unlinked name
    correctly returns an empty URL and falls back to name/club/order identity.
    """
    expected = _key(expected_name)
    if not expected:
        return ""
    clone = _clean_fragment(tag, remove_nested_lists=True)
    for anchor in clone.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        text = _normalise_space(anchor.get_text(" ", strip=True))
        if not href or href.startswith("#") or not text:
            continue
        if _key(text) != expected:
            continue
        if href.startswith("/wiki/") or "wikipedia.org/wiki/" in href:
            return urljoin(ARTICLE_URL, href)
    return ""


def _heading_name(h3: Any) -> str:
    headline = h3.select_one(".mw-headline")
    return _normalise_space((headline or h3).get_text(" ", strip=True))


def _heading_container(h3: Any) -> Any:
    parent = getattr(h3, "parent", None)
    classes = set(parent.get("class", [])) if parent and hasattr(parent, "get") else set()
    if parent is not None and any(cls.startswith("mw-heading") for cls in classes):
        return parent
    return h3


def _is_section_boundary(tag: Any) -> bool:
    if not getattr(tag, "name", None):
        return False
    if tag.name in {"h2", "h3"}:
        return True
    classes = set(tag.get("class", [])) if hasattr(tag, "get") else set()
    if "mw-heading2" in classes or "mw-heading3" in classes:
        return True
    return bool(tag.find(["h2", "h3"], recursive=False))


def _section_nodes(h3: Any) -> list[Any]:
    nodes: list[Any] = []
    for sibling in _heading_container(h3).next_siblings:
        if _is_section_boundary(sibling):
            break
        if getattr(sibling, "name", None):
            nodes.append(sibling)
    return nodes


def _top_lists(nodes: Iterable[Any]) -> Iterator[Any]:
    for node in nodes:
        if getattr(node, "name", None) in {"ul", "ol"}:
            yield node
            continue
        for candidate in node.find_all(["ul", "ol"]):
            if candidate.find_parent(["ul", "ol"]) is None:
                yield candidate


def _notes(nodes: Iterable[Any]) -> str:
    parts: list[str] = []
    for node in nodes:
        if getattr(node, "name", None) == "p":
            text = _visible_text(node)
            if text and not text.casefold().startswith("main article:"):
                parts.append(text)
    return _normalise_space(" ".join(parts))


# Club words that appear after a person's name without brackets.  Only used
# by ``_trailing_club_split``, which additionally requires the whole tail to
# parse as a club list, so ordinary surnames are never split off.
CLUB_VOCAB = [
    "adelaide", "brisbane bears", "brisbane lions", "brisbane", "carlton",
    "collingwood", "essendon", "fitzroy", "footscray", "fremantle",
    "geelong", "gold coast", "greater western sydney", "gws", "hawthorn",
    "kangaroos", "melbourne", "north melbourne", "port adelaide", "richmond",
    "south melbourne", "st kilda", "sydney", "university", "west coast",
    "western bulldogs",
    # State-league and second-tier clubs used throughout the article.
    "box hill", "casey", "coburg", "frankston", "port melbourne",
    "williamstown", "werribee", "sandringham", "north ballarat",
    "norwood", "glenelg", "sturt", "west adelaide", "central district",
    "woodville", "woodville-west torrens", "south adelaide", "north adelaide",
    "port adelaide magpies", "east perth", "west perth", "south fremantle",
    "east fremantle", "claremont", "subiaco", "swan districts", "perth",
    "peel thunder", "tasmania", "north hobart", "glenorchy", "clarence",
]

_CLUB_ALT = "|".join(
    sorted((re.escape(word) for word in CLUB_VOCAB), key=len, reverse=True)
)
# A tail is only treated as clubs when it is *entirely* club names joined by
# commas, slashes, "and" or "&".  "Sydney Coventry" therefore stays a name.
_CLUB_TAIL_RE = re.compile(
    rf"^(?:{_CLUB_ALT})"
    rf"(?:\s*(?:,|/|&|and)\s*(?:{_CLUB_ALT}))*"
    r"[\s.,;]*$",
    re.I,
)
_CLUB_START_RE = re.compile(rf"(?<![A-Za-z])(?:{_CLUB_ALT})(?![A-Za-z])", re.I)

# A relation label written without its colon, e.g. "Son Jed Bews".  The label
# must be followed by a capitalised name of at least two words.
_BARE_LABEL_RE = re.compile(
    r"^(?P<label>"
    + "|".join(
        sorted((re.escape(label) for label in LIST_RELATIONS), key=len, reverse=True)
    )
    + r")\s+(?P<rest>[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*)+.*)$",
    re.I,
)


def _strip_relation_label(text: str) -> tuple[str, str]:
    """Return ``(remaining_text, label)`` for a leading relation label."""
    match = RELATION_LABEL_RE.match(_key_label_text(text))
    if match:
        label = _normalise_relation_label(match.group("label"))
        # Apply the match against the original text as punctuation/case were
        # flattened only to identify the prefix.
        original = re.match(r"^[^:]+:\s*(.+)$", text)
        if original:
            text = original.group(1).strip()
        return text, label

    # Fault 1: the colon is missing in the source markup.  Only accepted when
    # no colon exists anywhere in the item, so ordinary labelled items keep
    # using the stricter rule above.
    if ":" not in text:
        bare = _BARE_LABEL_RE.match(text)
        if bare:
            label = _normalise_relation_label(bare.group("label"))
            if label in LIST_RELATIONS:
                return bare.group("rest").strip(), label
    return text, ""


def _trailing_club_split(text: str) -> tuple[str, str]:
    """Split an unbracketed trailing club list off the end of a name."""
    for match in _CLUB_START_RE.finditer(text):
        start = match.start()
        if start == 0:
            continue
        head = text[:start].strip(" ,;-–—")
        tail = text[start:].strip()
        # A person needs a plausible full name in front of the club words.
        if len(head.split()) < 2:
            continue
        if _CLUB_TAIL_RE.match(tail):
            return head, _normalise_space(tail.rstrip(" .,;"))
    return text, ""


def _split_clubs(text: str) -> tuple[str, str]:
    """Return ``(name_text, clubs_raw)`` for a member item."""
    # Normal case: club list is the final balanced parenthetical block.  Keep
    # the raw wording because it can contain historical/state-league clubs.
    club_match = re.search(r"\s*\(([^()]*)\)\s*$", text)
    if club_match:
        return (
            text[: club_match.start()].strip(),
            _normalise_space(club_match.group(1)),
        )

    # Fault 3: brackets are unbalanced, e.g.
    # "Norm Collins ( Fitzroy ), Carlton and Hawthorn )".  Everything from the
    # first bracket is club text; the brackets themselves are discarded.
    if text.count("(") != text.count(")"):
        first = text.find("(")
        if first > 0:
            clubs = text[first:].replace("(", " ").replace(")", " ")
            clubs = _normalise_space(clubs)
            clubs = re.sub(r"\s+([,;])", r"\1", clubs).strip(" .,;")
            return text[:first].strip(), clubs

    # Fault 2: the club list is not bracketed at all, e.g.
    # "Tim Callan Geelong , Western Bulldogs".
    head, clubs = _trailing_club_split(text)
    if clubs:
        clubs = re.sub(r"\s+([,;])", r"\1", clubs)
        return head, clubs

    return text, ""


def _parse_li_text(li: Any) -> tuple[str, str, str]:
    text = _visible_text(li, remove_nested_lists=True)
    text, label = _strip_relation_label(text)
    text, clubs = _split_clubs(text)
    name = re.sub(r"\s*[;,.]+\s*$", "", text).strip()
    return name, clubs, label


def _key_label_text(value: str) -> str:
    """Normalise only the relation label while preserving the colon."""
    if ":" not in value:
        return value
    label, rest = value.split(":", 1)
    return f"{_normalise_relation_label(label)}: {rest.strip()}"


def _normalise_relation_label(value: object) -> str:
    text = _key(value).replace("-", " ")
    text = re.sub(r"\b(?:older|elder|younger)\b", "", text)
    return _normalise_space(text)


def _member_identity(name: str, url: str, clubs: str, order: int) -> str:
    # A source member is a list occurrence, not merely a Wikipedia page.
    # Include name and order even when linked so malformed/repeated links can
    # never collapse two source rows onto one source_member_id.
    if url:
        return (f"url:{url.casefold()}|name:{_key(name)}|"
                f"clubs:{_key(clubs)}|order:{order}")
    # Same-name people can occur in one family. Club text separates most;
    # order remains the safe final discriminator.
    return f"name:{_key(name)}|clubs:{_key(clubs)}|order:{order}"


def _relationship_id(
    family_key: str,
    a_id: str,
    b_id: str,
    relationship_type: str,
    a_role: str,
    b_role: str,
    method: str,
) -> str:
    return _sha(
        family_key, a_id, b_id, relationship_type, a_role, b_role, method
    )


def _make_relationship(
    *,
    family_key: str,
    family_name: str,
    a: Member,
    b: Member,
    relationship_type: str,
    relationship_label: str,
    a_role: str,
    b_role: str,
    evidence: str,
    method: str,
    confidence: str,
    revision_id: int | str,
    scraped_at: str,
) -> Relationship:
    return Relationship(
        source_relationship_id=_relationship_id(
            family_key,
            a.source_member_id,
            b.source_member_id,
            relationship_type,
            a_role,
            b_role,
            method,
        ),
        family_key=family_key,
        family_name=family_name,
        person_a_source_member_id=a.source_member_id,
        person_a_name=a.member_name,
        person_a_role=a_role,
        person_b_source_member_id=b.source_member_id,
        person_b_name=b.member_name,
        person_b_role=b_role,
        relationship_type=relationship_type,
        relationship_label=relationship_label,
        evidence=evidence,
        extraction_method=method,
        confidence=confidence,
        source_url=ARTICLE_URL,
        source_revision_id=revision_id,
        scraped_at_utc=scraped_at,
    )


def _walk_list(
    list_tag: Any,
    *,
    family_key: str,
    family_name: str,
    family_notes: str,
    revision_id: int | str,
    scraped_at: str,
    order_counter: list[int],
    members: list[Member],
    relationships: list[Relationship],
    parent: Member | None = None,
    depth: int = 0,
) -> None:
    for li in list_tag.find_all("li", recursive=False):
        name, clubs, label = _parse_li_text(li)
        if not name or name.casefold().startswith("main article"):
            continue
        order_counter[0] += 1
        member_order = order_counter[0]
        url = _first_wiki_url(li, name)
        source_member_id = _sha(
            family_key, _member_identity(name, url, clubs, member_order)
        )
        member = Member(
            source_member_id=source_member_id,
            family_key=family_key,
            family_name=family_name,
            member_name=name,
            member_wikipedia_url=url,
            clubs_raw=clubs,
            list_depth=depth,
            member_order=member_order,
            parent_source_member_id=parent.source_member_id if parent else "",
            explicit_relation_label=label,
            family_notes=family_notes,
            source_url=ARTICLE_URL,
            source_revision_id=revision_id,
            scraped_at_utc=scraped_at,
        )
        members.append(member)

        if parent and label in LIST_RELATIONS:
            rel_type, a_role, b_role, rel_label = LIST_RELATIONS[label]
            relationships.append(
                _make_relationship(
                    family_key=family_key,
                    family_name=family_name,
                    a=parent,
                    b=member,
                    relationship_type=rel_type,
                    relationship_label=rel_label,
                    a_role=a_role,
                    b_role=b_role,
                    evidence=f"{label.title()}: {member.member_name}",
                    method="list_label",
                    confidence="high",
                    revision_id=revision_id,
                    scraped_at=scraped_at,
                )
            )

        for child_list in li.find_all(["ul", "ol"], recursive=False):
            _walk_list(
                child_list,
                family_key=family_key,
                family_name=family_name,
                family_notes=family_notes,
                revision_id=revision_id,
                scraped_at=scraped_at,
                order_counter=order_counter,
                members=members,
                relationships=relationships,
                parent=member,
                depth=depth + 1,
            )


def _name_aliases(members: list[Member]) -> tuple[dict[str, Member], re.Pattern[str] | None]:
    candidates: dict[str, list[Member]] = defaultdict(list)
    for member in members:
        full = _key(member.member_name)
        if not full:
            continue
        words = full.split()
        aliases = {full}
        if words:
            aliases.add(words[0])
        if len(words) >= 2:
            aliases.add(words[-1])
            aliases.add(" ".join(words[:2]))
        # Common prose form for generational suffixes.
        suffix_map = {"jr": "junior", "jnr": "junior", "sr": "senior", "snr": "senior"}
        if words and words[-1] in suffix_map:
            aliases.add(" ".join(words[:-1] + [suffix_map[words[-1]]]))
            aliases.add(f"{words[0]} {suffix_map[words[-1]]}")
        for alias in aliases:
            if len(alias) >= 2:
                candidates[alias].append(member)

    unique = {alias: rows[0] for alias, rows in candidates.items() if len(rows) == 1}

    # When a family contains Senior/Junior names, prose commonly calls the
    # older person by the bare first name and the younger one "First Junior".
    # Recover those aliases only when the suffix pattern makes the choice
    # unambiguous; ordinary duplicated first names remain deliberately unset.
    by_first: dict[str, list[Member]] = defaultdict(list)
    for member in members:
        words = _key(member.member_name).split()
        if words:
            by_first[words[0]].append(member)
    for first, rows in by_first.items():
        if len(rows) < 2:
            continue
        junior = [row for row in rows if _key(row.member_name).split()[-1:] in (["jr"], ["jnr"], ["junior"])]
        senior = [row for row in rows if _key(row.member_name).split()[-1:] in (["sr"], ["snr"], ["senior"])]
        if len(junior) == 1:
            unique[f"{first} junior"] = junior[0]
            unique[f"{first} jr"] = junior[0]
        if len(senior) == 1:
            unique[f"{first} senior"] = senior[0]
            unique[f"{first} sr"] = senior[0]
            if len(junior) == 1 and len(rows) == 2:
                unique[first] = senior[0]

    if not unique:
        return unique, None
    pattern = re.compile(
        r"(?<![a-z0-9])(" + "|".join(
            sorted((re.escape(alias) for alias in unique), key=len, reverse=True)
        ) + r")(?![a-z0-9])",
        re.I,
    )
    return unique, pattern


def _tokenise_sentence(
    sentence: str, aliases: dict[str, Member], pattern: re.Pattern[str] | None
) -> tuple[str, list[Member]]:
    if pattern is None:
        return _key(sentence), []
    used: list[Member] = []
    index_by_id: dict[str, int] = {}

    def repl(match: re.Match[str]) -> str:
        alias = _key(match.group(0))
        member = aliases[alias]
        if member.source_member_id not in index_by_id:
            index_by_id[member.source_member_id] = len(used)
            used.append(member)
        return f" §{index_by_id[member.source_member_id]}§ "

    ascii_sentence = _ascii(sentence).casefold()
    tokenised = pattern.sub(repl, ascii_sentence)
    tokenised = re.sub(r"[^a-z0-9§',&-]+", " ", tokenised)
    return _normalise_space(tokenised), used


def _members_from_token_text(text: str, used: list[Member]) -> list[Member]:
    out: list[Member] = []
    seen: set[str] = set()
    for number in TOKEN_RE.findall(text):
        member = used[int(number)]
        if member.source_member_id not in seen:
            seen.add(member.source_member_id)
            out.append(member)
    return out


def _pairwise(items: list[Member]) -> Iterator[tuple[Member, Member]]:
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            yield left, right


def _prose_relationships(
    family_key: str,
    family_name: str,
    members: list[Member],
    notes: str,
    revision_id: int | str,
    scraped_at: str,
) -> list[Relationship]:
    if not notes or len(members) < 2:
        return []

    aliases, pattern = _name_aliases(members)
    output: list[Relationship] = []

    def add(
        a: Member,
        b: Member,
        rel_type: str,
        label: str,
        a_role: str,
        b_role: str,
        sentence: str,
    ) -> None:
        # A short prose alias can occasionally match inside a longer name.
        # When both sides then resolve to the same source member, the sentence
        # has not established a relationship between two people. Discard it
        # rather than weakening validation or importing a self-link.
        if a.source_member_id == b.source_member_id:
            return
        output.append(
            _make_relationship(
                family_key=family_key,
                family_name=family_name,
                a=a,
                b=b,
                relationship_type=rel_type,
                relationship_label=label,
                a_role=a_role,
                b_role=b_role,
                evidence=sentence,
                method="prose_rule",
                confidence="high",
                revision_id=revision_id,
                scraped_at=scraped_at,
            )
        )

    for sentence in SENTENCE_SPLIT_RE.split(notes):
        sentence = _normalise_space(sentence)
        if not sentence:
            continue
        text, used = _tokenise_sentence(sentence, aliases, pattern)
        if not used:
            continue

        # "A, B and C were brothers/sisters/twins."
        group = re.search(
            r"((?:§\d+§(?:\s*(?:,|and|&)\s*)?)+)\s+"
            r"(?:were|are)\s+(?:all\s+)?(brothers|sisters|siblings|twins)\b",
            text,
        )
        if group:
            relation_word = group.group(2)
            group_members = _members_from_token_text(group.group(1), used)
            label = "twins" if relation_word == "twins" else (
                "siblings/brothers" if relation_word == "brothers" else "siblings"
            )
            role = "twin" if relation_word == "twins" else "sibling"
            for a, b in _pairwise(group_members):
                add(a, b, "sibling", label, role, role, sentence)

        # "A is the elder brother/sister of B [and C]."
        for match in re.finditer(
            r"(§\d+§)\s+(?:is|was)\s+(?:the\s+)?(?:elder\s+|older\s+|younger\s+)?"
            r"(brother|sister|sibling)\s+of\s+"
            r"((?:§\d+§(?:\s*(?:,|and|&)\s*)?)+)",
            text,
        ):
            left = _members_from_token_text(match.group(1), used)
            right = _members_from_token_text(match.group(3), used)
            for a in left:
                for b in right:
                    add(
                        a,
                        b,
                        "sibling",
                        "siblings/brothers" if match.group(2) == "brother" else "siblings",
                        match.group(2),
                        "sibling",
                        sentence,
                    )

        # "A is B's brother/sister/father/mother/grandfather/..."
        for match in re.finditer(
            r"(§\d+§)\s+(?:is|was)\s+(§\d+§)'s\s+"
            r"(brother|sister|father|mother|parent|grandfather|grandmother|"
            r"husband|wife|spouse|cousin)",
            text,
        ):
            a_rows = _members_from_token_text(match.group(1), used)
            b_rows = _members_from_token_text(match.group(2), used)
            if not a_rows or not b_rows:
                continue
            a, b, role = a_rows[0], b_rows[0], match.group(3)
            if role in {"brother", "sister"}:
                add(a, b, "sibling", "siblings/brothers" if role == "brother" else "siblings", role, "sibling", sentence)
            elif role in {"father", "mother", "parent"}:
                add(a, b, "parent_child", f"{role}-child", role, "child", sentence)
            elif role in {"grandfather", "grandmother"}:
                add(a, b, "grandparent_grandchild", f"{role}-grandchild", role, "grandchild", sentence)
            elif role in {"husband", "wife", "spouse"}:
                add(a, b, "spouse", "spouses", role, "spouse", sentence)
            else:
                add(a, b, "cousin", "cousins", "cousin", "cousin", sentence)

        # "A is/was the father/mother/parent/grandfather of B and C."
        for match in re.finditer(
            r"(§\d+§)\s+(?:is|was)\s+(?:the\s+)?"
            r"(father|mother|parent|grandfather|grandmother)\s+of\s+"
            r"((?:§\d+§(?:\s*(?:,|and|&)\s*)?)+)",
            text,
        ):
            parents = _members_from_token_text(match.group(1), used)
            children = _members_from_token_text(match.group(3), used)
            role = match.group(2)
            for a in parents:
                for b in children:
                    if role.startswith("grand"):
                        add(a, b, "grandparent_grandchild", f"{role}-grandchild", role, "grandchild", sentence)
                    else:
                        child_role = "son" if " son" in text else (
                            "daughter" if " daughter" in text else "child"
                        )
                        label = "father-son/parent-son" if role == "father" and child_role == "son" else f"{role}-{child_role}"
                        add(a, b, "parent_child", label, role, child_role, sentence)

        # "A and B are the sons/daughters/children of C."
        for match in re.finditer(
            r"((?:§\d+§(?:\s*(?:,|and|&)\s*)?)+)\s+"
            r"(?:are|were)\s+(?:the\s+)?(sons|daughters|children)\s+of\s+(§\d+§)",
            text,
        ):
            children = _members_from_token_text(match.group(1), used)
            parents = _members_from_token_text(match.group(3), used)
            child_role = {"sons": "son", "daughters": "daughter", "children": "child"}[match.group(2)]
            for parent in parents:
                for child in children:
                    add(
                        parent,
                        child,
                        "parent_child",
                        "father-son/parent-son" if child_role == "son" else f"parent-{child_role}",
                        "parent",
                        child_role,
                        sentence,
                    )

        # "A is the son/daughter/child of B."
        for match in re.finditer(
            r"(§\d+§)\s+(?:is|was)\s+(?:the\s+)?(son|daughter|child)\s+of\s+(§\d+§)",
            text,
        ):
            children = _members_from_token_text(match.group(1), used)
            parents = _members_from_token_text(match.group(3), used)
            for parent in parents:
                for child in children:
                    role = match.group(2)
                    add(
                        parent,
                        child,
                        "parent_child",
                        "father-son/parent-son" if role == "son" else f"parent-{role}",
                        "parent",
                        role,
                        sentence,
                    )

        # "A and B are/were married."
        for match in re.finditer(
            r"((?:§\d+§(?:\s*(?:,|and|&)\s*)?)+)\s+(?:are|were)\s+married\b",
            text,
        ):
            spouses = _members_from_token_text(match.group(1), used)
            for a, b in _pairwise(spouses):
                add(a, b, "spouse", "spouses", "spouse", "spouse", sentence)

    # Conservative fallback for the very common two-person family section
    # where a nickname prevents an alias match (e.g. Clarence/Clarrie).
    roots = [member for member in members if member.list_depth == 0]
    if len(roots) == 2 and re.search(r"\b(?:were|are)\s+brothers\b", notes, re.I):
        add(
            roots[0], roots[1], "sibling", "siblings/brothers", "brother", "brother", notes
        )
    return output


def _dedupe_relationships(rows: Iterable[Relationship]) -> list[Relationship]:
    chosen: dict[tuple[str, str, str, str], Relationship] = {}
    method_rank = {"list_label": 0, "prose_rule": 1}
    for row in rows:
        # Defensive second boundary: only relationships between distinct
        # source members can enter the trusted output, regardless of which
        # extraction rule produced them.
        if row.person_a_source_member_id == row.person_b_source_member_id:
            continue
        # Symmetric relationships are deduped without direction.  Directional
        # types keep the role-bearing orientation.
        if row.relationship_type in {"sibling", "cousin", "spouse"}:
            pair = tuple(sorted((row.person_a_source_member_id, row.person_b_source_member_id)))
        else:
            pair = (row.person_a_source_member_id, row.person_b_source_member_id)
        key = (row.family_key, pair[0], pair[1], row.relationship_type)
        previous = chosen.get(key)
        if previous is None or method_rank.get(row.extraction_method, 9) < method_rank.get(previous.extraction_method, 9):
            chosen[key] = row
    return sorted(
        chosen.values(),
        key=lambda row: (
            row.family_name.casefold(),
            row.person_a_name.casefold(),
            row.person_b_name.casefold(),
            row.relationship_type,
        ),
    )


def parse_payload(
    payload: dict[str, Any], scraped_at: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScrapeError(
            "Missing dependency: beautifulsoup4. Run: pip install beautifulsoup4"
        ) from exc

    parsed = payload.get("parse", {})
    html = parsed.get("text")
    if not isinstance(html, str) or not html.strip():
        raise ScrapeError("Cached/API payload contains no parse.text HTML")
    revision_id = parsed.get("revid", "")
    soup = BeautifulSoup(html, "html.parser")

    all_members: list[Member] = []
    all_relationships: list[Relationship] = []
    section_count = 0
    skipped: list[str] = []

    for h3 in soup.find_all("h3"):
        family_name = _heading_name(h3)
        if not family_name or family_name.casefold() in {"sources", "references", "see also", "external links"}:
            continue
        nodes = _section_nodes(h3)
        lists = list(_top_lists(nodes))
        if not lists:
            continue
        notes = _notes(nodes)
        section_count += 1
        family_key = f"{_slug(family_name)}-{section_count:04d}"
        section_members: list[Member] = []
        section_relationships: list[Relationship] = []
        order_counter = [0]
        for list_tag in lists:
            _walk_list(
                list_tag,
                family_key=family_key,
                family_name=family_name,
                family_notes=notes,
                revision_id=revision_id,
                scraped_at=scraped_at,
                order_counter=order_counter,
                members=section_members,
                relationships=section_relationships,
            )
        if len(section_members) < 2:
            skipped.append(family_name)
            continue
        section_relationships.extend(
            _prose_relationships(
                family_key,
                family_name,
                section_members,
                notes,
                revision_id,
                scraped_at,
            )
        )
        all_members.extend(section_members)
        all_relationships.extend(section_relationships)

    relationships = _dedupe_relationships(all_relationships)
    info = {
        "page_title": parsed.get("title", PAGE_TITLE),
        "display_title": parsed.get("displaytitle", ""),
        "revision_id": revision_id,
        "families": len({row.family_key for row in all_members}),
        "members": len(all_members),
        "relationships": len(relationships),
        "relationship_types": dict(Counter(row.relationship_type for row in relationships)),
        "extraction_methods": dict(Counter(row.extraction_method for row in relationships)),
        "skipped_sections_with_fewer_than_two_members": skipped,
    }
    return (
        [row.asdict() for row in all_members],
        [row.asdict() for row in relationships],
        info,
    )


def validate(
    members: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    min_families: int,
    min_members: int,
    min_relationships: int,
) -> dict[str, int]:
    families = {row["family_key"] for row in members}
    counts = {
        "families": len(families),
        "members": len(members),
        "relationships": len(relationships),
    }
    if counts["families"] < min_families:
        raise ScrapeError(
            f"Validation failed: {counts['families']} families; expected at least {min_families}"
        )
    if counts["members"] < min_members:
        raise ScrapeError(
            f"Validation failed: {counts['members']} members; expected at least {min_members}"
        )
    if counts["relationships"] < min_relationships:
        raise ScrapeError(
            "Validation failed: "
            f"{counts['relationships']} explicit relationships; expected at least {min_relationships}"
        )

    member_ids = [row["source_member_id"] for row in members]
    if len(member_ids) != len(set(member_ids)):
        raise ScrapeError("Validation failed: duplicate source_member_id values")
    known = set(member_ids)
    for row in relationships:
        if row["person_a_source_member_id"] not in known or row["person_b_source_member_id"] not in known:
            raise ScrapeError(
                f"Validation failed: relationship references unknown member {row['source_relationship_id']}"
            )
        if row["person_a_source_member_id"] == row["person_b_source_member_id"]:
            raise ScrapeError(
                f"Validation failed: self relationship {row['source_relationship_id']}"
            )
    return counts


def render_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    raw = _default_raw_dir()
    cache_dir = _default_cache_dir()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--members-output",
        type=Path,
        default=raw / "wikipedia_family_members.csv",
    )
    parser.add_argument(
        "--relationships-output",
        type=Path,
        default=raw / "wikipedia_family_relationships.csv",
    )
    parser.add_argument(
        "--metadata-output",
        type=Path,
        default=raw / "wikipedia_families.metadata.json",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=cache_dir / "mediawiki_parse.json",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--min-families", type=int, default=200)
    parser.add_argument("--min-members", type=int, default=500)
    parser.add_argument("--min-relationships", type=int, default=120)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    if args.refresh and args.cache.exists() and not args.offline:
        args.cache.unlink()

    scraped_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        payload, fetched_from = fetch_api_payload(
            args.cache,
            offline=args.offline,
            timeout=args.timeout,
            no_cache=args.no_cache,
        )
        members, relationships, info = parse_payload(payload, scraped_at)
        counts = validate(
            members,
            relationships,
            min_families=args.min_families,
            min_members=args.min_members,
            min_relationships=args.min_relationships,
        )
    except (OSError, ScrapeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    members_csv = render_csv(members, MEMBER_COLUMNS)
    relationships_csv = render_csv(relationships, RELATIONSHIP_COLUMNS)
    _atomic_write_bytes(args.members_output, members_csv)
    _atomic_write_bytes(args.relationships_output, relationships_csv)

    metadata = {
        **info,
        "source_url": ARTICLE_URL,
        "fetched_from": fetched_from,
        "scraped_at_utc": scraped_at,
        "members_output": str(args.members_output),
        "relationships_output": str(args.relationships_output),
        "members_sha256": hashlib.sha256(members_csv).hexdigest(),
        "relationships_sha256": hashlib.sha256(relationships_csv).hexdigest(),
        "validation": counts,
        "licence_note": (
            "Wikipedia text is available under CC BY-SA; retain source URL and "
            "revision metadata when using or redistributing derived rows."
        ),
    }
    _atomic_write_json(args.metadata_output, metadata)

    print(
        f"Saved {counts['members']:,} members across {counts['families']:,} families"
    )
    print(f"Saved {counts['relationships']:,} explicit relationships")
    print(f"Members:       {args.members_output}")
    print(f"Relationships: {args.relationships_output}")
    print(f"Metadata:      {args.metadata_output}")
    print(f"Wikipedia revision: {info.get('revision_id') or 'unknown'} ({fetched_from})")

    if args.report:
        print("\nRelationship types:")
        for label, count in sorted(info["relationship_types"].items()):
            print(f"  {label:<28} {count:>5,}")
        print("Extraction methods:")
        for label, count in sorted(info["extraction_methods"].items()):
            print(f"  {label:<28} {count:>5,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
