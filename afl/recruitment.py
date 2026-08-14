"""Reading Draftguru's recruitment path -- where a player came from.

`draft.original_club` is not one club. It is the path a player took to the
draft, junior end first, written as segments separated by ` / `::

    Whitford JFC / West Perth
    Greythorn / Xavier College / Oakleigh U18
    Scots (Albury) / Albury FC / Murray U18 / Western Sydney University

6,549 of the 6,810 draft records carry one, between one and eight segments
long, and the separator is exactly ` / ` in every multi-segment path in the
database -- which is what lets a segment be matched exactly in SQL rather
than by substring, where "Trinity College" would swallow "Trinity College
(WA)".

The ordering is the useful part and it comes free with the data: the last
segment is the club a player was drafted *from*, the first is usually where
they started. Nothing here reorders or dedupes a path; a player who
returned to a junior club is recorded that way by the source and it is not
this module's place to disagree.

`kind()` sorts a segment into one of three buckets, and deliberately stops
there. `U18` names the sixteen talent-league clubs and nothing else in the
table; the school words match 144 segments, every one of them a school. A
segment that is neither is a club, which is both the truth for the great
majority and the honest answer for the 334 that appear exactly once. There
is no attempt to tell a WAFL club from a suburban one: the source does not
say, and guessing from the name would be wrong often enough to matter.
"""

from __future__ import annotations

import re

SEPARATOR = " / "

#: The talent-league clubs -- Sandringham, Oakleigh, Murray and the rest of
#: the under-18 pathway. Sixteen of them, every one suffixed `U18`.
TALENT_LEAGUE = "Talent league"
#: A school or college. Xavier, Haileybury, Scotch, Prince Alfred.
SCHOOL = "School"
#: Everything else, from a WAFL club to a suburban junior side.
CLUB = "Club"

_TALENT_LEAGUE = re.compile(r"\bU18\b")
_SCHOOL = re.compile(r"(?i)\b(college|grammar|school|secondary|high)\b")


def path(value) -> list[str]:
    """The ordered segments of a recruitment path, junior end first.

    Empty for a record with no path -- 261 of them, mostly the older
    drafts, where the source simply did not record one.
    """
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split("/") if part.strip()]


def drafted_from(value):
    """The last step of the path: the club a player reached the AFL from.

    For a single-segment path that is the whole path, which is correct --
    a West Australian listed only as "Claremont" was drafted from
    Claremont.
    """
    parts = path(value)
    return parts[-1] if parts else None


def junior_club(value):
    """The first step: where the source says a player started."""
    parts = path(value)
    return parts[0] if parts else None


def kind(segment) -> str:
    """`TALENT_LEAGUE`, `SCHOOL` or `CLUB` for one segment.

    Tested in that order because a school can sit in a talent-league
    club's name in principle, and the pathway is the more specific fact.
    """
    text = str(segment or "")
    if _TALENT_LEAGUE.search(text):
        return TALENT_LEAGUE
    if _SCHOOL.search(text):
        return SCHOOL
    return CLUB


def segment_or_prefix_sql(column: str) -> str:
    """Like `segment_match_sql`, but a term may also start a segment.

    For text somebody typed rather than picked. "Oakleigh" is what a
    person writes when they mean the Oakleigh Chargers, whose segment is
    "Oakleigh U18", and a whole-segment match would answer nothing. The
    prefix is anchored to the start of a segment, so "Geelong" reaches
    "Geelong U18" and "Geelong College" but never "North Geelong".

    A term that is itself a whole segment matches only that segment, so
    anything picked from a list behaves exactly as `segment_match_sql`.

    Takes the term twice.
    """
    wrapped = f"'{SEPARATOR}' || {column} || '{SEPARATOR}'"
    return (f"({wrapped} LIKE '%{SEPARATOR}' || ? || '{SEPARATOR}%' "
            f"OR {wrapped} LIKE '%{SEPARATOR}' || ? || ' %')")


def segment_match_sql(column: str) -> str:
    """A SQL fragment matching one whole segment of `column`'s path.

    Substring matching is wrong here: "Geelong" appears inside "Geelong
    U18", "Geelong College" and "Geelong Falcons", which are three
    different places. Wrapping both the column and the term in the
    separator pins the match to segment boundaries, the same trick the
    club-list columns elsewhere use with commas.

    Takes one parameter, the segment name.
    """
    return (f"'{SEPARATOR}' || {column} || '{SEPARATOR}' "
            f"LIKE '%{SEPARATOR}' || ? || '{SEPARATOR}%'")
