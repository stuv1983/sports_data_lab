"""The one spelling a stored date of birth may have: ISO ``YYYY-MM-DD``.

Two spellings coexisted for years. The fitzRoy build copied AFL Tables'
``30-Jan-1987`` text into ``players.dob`` and ``games.dob`` verbatim,
while the profile loader wrote ISO -- so the columns mixed both, no date
declaration was truthful, and the query builder had to treat a birth
date as free text. Every loader now funnels through canonical_dob() on
the way in, utils/afl/normalize_dob.py rewrote the rows already stored,
and afl/sport.py declares the columns as dates again.

The club-page register keeps its scraped spelling: those rows belong to
the scrape, and only the *comparison* against players.dob needs the
canonical form (see load_club_sources.link_record).
"""
from __future__ import annotations

from datetime import datetime

#: The stored spellings a date of birth has ever legitimately had. ISO
#: first: it is the target form, and after migration the overwhelmingly
#: common one.
_KNOWN_FORMATS = ("%Y-%m-%d", "%d-%b-%Y")


def canonical_dob(text) -> str | None:
    """A date of birth in any stored spelling -> ISO, else None.

    None for anything that is not a real calendar date -- empty text,
    pandas' stringified ``nan``, or malformed values. Never returns the
    input unparsed: a value this function cannot read must become NULL
    rather than survive as text masquerading as a date.
    """
    raw = str(text or "").strip()
    if not raw or raw.lower() in ("nan", "none", "nat"):
        return None
    for spelling in _KNOWN_FORMATS:
        try:
            return datetime.strptime(raw, spelling).date().isoformat()
        except ValueError:
            continue
    return None
