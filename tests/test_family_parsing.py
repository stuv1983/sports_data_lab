#!/usr/bin/env python3
"""Regression fixtures for family hotfix 1.

Covers the three source-name parsing faults in the Wikipedia family scraper
and the Gridley criterion mapping for family squares.

Run:
    python test_family_parsing.py
"""

from __future__ import annotations

# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from afl import parse_criteria as P
from afl import scrape_wikipedia_families as S


def _li(html: str):
    return BeautifulSoup(f"<ul>{html}</ul>", "html.parser").find("li")


def _parse(html: str) -> tuple[str, str, str]:
    return S._parse_li_text(_li(html))


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Fault 1 -- relation label written without a colon.
# ---------------------------------------------------------------------------

def test_bare_relation_label() -> None:
    print("fault 1: relation label without a colon")
    check(
        "Son Jed Bews",
        _parse('<li>Son <a href="/wiki/Jed_Bews">Jed Bews</a> (Geelong)</li>'),
        ("Jed Bews", "Geelong", "son"),
    )
    check(
        "Cousin without brackets",
        _parse('<li>Cousin <a href="/wiki/Sam_Smith">Sam Smith</a></li>'),
        ("Sam Smith", "", "cousin"),
    )
    check(
        "labelled item with a colon still uses the strict rule",
        _parse('<li>Son: <a href="/wiki/James_Aish">James Aish</a> (Collingwood)</li>'),
        ("James Aish", "Collingwood", "son"),
    )
    # A first name that merely starts with a label word must not be stripped.
    check(
        "Sonny Walters is not a 'son' label",
        _parse("<li>Sonny Walters (Richmond)</li>"),
        ("Sonny Walters", "Richmond", ""),
    )
    # A single trailing word is not a plausible full name, so no label strip.
    check(
        "one-word remainder is left alone",
        _parse("<li>Son Bews</li>"),
        ("Son Bews", "", ""),
    )


# ---------------------------------------------------------------------------
# Fault 2 -- unbracketed trailing club list absorbed into the name.
# ---------------------------------------------------------------------------

def test_unbracketed_clubs() -> None:
    print("fault 2: unbracketed trailing club list")
    check(
        "Tim Callan Geelong , Western Bulldogs",
        _parse("<li>Tim Callan Geelong , Western Bulldogs</li>"),
        ("Tim Callan", "Geelong, Western Bulldogs", ""),
    )
    check(
        "single unbracketed club",
        _parse("<li>Ron Barassi Melbourne</li>"),
        ("Ron Barassi", "Melbourne", ""),
    )
    check(
        "clubs joined with 'and'",
        _parse("<li>Bill Smith Carlton and Hawthorn</li>"),
        ("Bill Smith", "Carlton and Hawthorn", ""),
    )
    # The critical safety case: a club word used as a personal name.
    check(
        "Sydney Coventry is not split",
        _parse("<li>Sydney Coventry (Collingwood)</li>"),
        ("Sydney Coventry", "Collingwood", ""),
    )
    check(
        "Adelaide Smith Jones is not split (tail is not a club list)",
        _parse("<li>Bob Adelaide Smith</li>"),
        ("Bob Adelaide Smith", "", ""),
    )
    check(
        "bracketed clubs still win over the unbracketed rule",
        _parse("<li>Tim Callan (Geelong, Western Bulldogs)</li>"),
        ("Tim Callan", "Geelong, Western Bulldogs", ""),
    )


# ---------------------------------------------------------------------------
# Fault 3 -- unbalanced parentheses.
# ---------------------------------------------------------------------------

def test_unbalanced_brackets() -> None:
    print("fault 3: unbalanced parentheses")
    check(
        "Norm Collins ( Fitzroy ), Carlton and Hawthorn )",
        _parse("<li>Norm Collins ( Fitzroy ), Carlton and Hawthorn )</li>"),
        ("Norm Collins", "Fitzroy, Carlton and Hawthorn", ""),
    )
    check(
        "unclosed bracket",
        _parse("<li>Jack Green (Richmond, St Kilda</li>"),
        ("Jack Green", "Richmond, St Kilda", ""),
    )
    check(
        "balanced brackets are untouched",
        _parse("<li>Jack Green (Richmond)</li>"),
        ("Jack Green", "Richmond", ""),
    )


# ---------------------------------------------------------------------------
# Combined -- a label and a broken club block in one item.
# ---------------------------------------------------------------------------

def test_combined() -> None:
    print("combined faults in one item")
    check(
        "label plus unbalanced clubs",
        _parse("<li>Son Norm Collins ( Fitzroy ), Carlton )</li>"),
        ("Norm Collins", "Fitzroy, Carlton", "son"),
    )


# ---------------------------------------------------------------------------
# Member identity must stay stable and distinct after cleaning.
# ---------------------------------------------------------------------------

def test_member_identity_still_distinct() -> None:
    print("source member identity")
    a = S._member_identity("Jack Green", "", "Richmond", 1)
    b = S._member_identity("Jack Green", "", "Richmond", 2)
    check("same name and club, different order -> distinct", a != b, True)
    c = S._member_identity("Jack Green", "", "Richmond", 1)
    check("identical inputs -> identical id", a == c, True)


# ---------------------------------------------------------------------------
# Gridley criterion mapping.
# ---------------------------------------------------------------------------

def _label(text: str):
    constraint, label = P.parse(text)
    return (constraint is not None, label)


def test_family_criteria() -> None:
    print("gridley family criteria")
    check("BROTHER PLAYED", _label("BROTHER PLAYED"),
          (True, "brother also played"))
    check("SIBLING PLAYED", _label("SIBLING PLAYED"),
          (True, "sibling also played"))
    check("FATHER PLAYED", _label("FATHER PLAYED"),
          (True, "father/son also played"))
    check("DAD PLAYED", _label("DAD PLAYED"),
          (True, "father/son also played"))
    check("SON PLAYED", _label("SON PLAYED"),
          (True, "father/son also played"))
    check("COUSIN PLAYED", _label("COUSIN PLAYED"),
          (True, "extended family also played"))
    check("GRANDFATHER PLAYED", _label("GRANDFATHER PLAYED"),
          (True, "extended family also played"))
    check("RELATIVE PLAYED", _label("RELATIVE PLAYED"),
          (True, "AFL/VFL family member"))
    check("AFL FAMILY", _label("AFL FAMILY"),
          (True, "AFL/VFL family member"))
    check("BROTHER PLAYED FOR GEELONG", _label("BROTHER PLAYED FOR GEELONG"),
          (True, "relative played for Geelong"))

    # Draft criteria must not be captured by the relationship layer.
    check("FATHER-SON SELECTION stays a draft criterion",
          _label("FATHER-SON SELECTION"), (True, "father-son selection"))

    # Unrelated squares are unaffected.
    check("150+ GAMES unaffected", P.parse("150+ GAMES PLAYED")[0] is not None, True)
    check("CARLTON unaffected", _label("CARLTON"), (True, "Carlton"))


def main() -> int:
    test_bare_relation_label()
    test_unbracketed_clubs()
    test_unbalanced_brackets()
    test_combined()
    test_member_identity_still_distinct()
    test_family_criteria()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failing check(s): {', '.join(FAILURES)}")
        return 1
    print("family parsing regression tests: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
