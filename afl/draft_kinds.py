"""
afl/draft_kinds.py -- Canonical draft categories, decided at ingestion.

Draftguru's own labels drift ("National" and "National Draft" both appear
on real year pages), so every query that asked "is this a national
selection?" used to run LIKE '%national%' over the raw label -- correct,
but a per-query substring scan restated in five places, and invisible to
any index. The category is now decided once, when the row is loaded, and
stored in draft.draft_kind; queries compare the bare column against these
constants, and ix_draft_kind can serve them.

The raw draft_type column is kept untouched beside it: it is what the
Draft Explorer browses and what a person recognises from the source page.
"""

import re

NATIONAL = "national"
ROOKIE = "rookie"
TRADE = "trade"
PRESEASON = "preseason"
MIDSEASON = "midseason"
FREE_AGENCY = "free_agency"

#: Substring -> canonical kind, first match wins. Substrings on purpose,
#: mirroring the LIKE predicates this replaces, so upstream wording drift
#: ("National" vs "National Draft") keeps landing in the same category.
_RULES = (
    ("national", NATIONAL),
    ("rookie", ROOKIE),
    ("trade", TRADE),
    ("free agency", FREE_AGENCY),
    ("pre-season", PRESEASON),
    ("preseason", PRESEASON),
    ("mid-season", MIDSEASON),
    ("midseason", MIDSEASON),
)


def draft_kind(draft_type) -> str | None:
    """The canonical category for one Draftguru draft-type label.

    A label no rule recognises becomes a stable lowercase slug
    ("Pre-Draft" -> "pre_draft") rather than None: it stays grouped and
    queryable without this module having to know every label Draftguru
    will ever print. None only for a genuinely blank label.
    """
    if draft_type is None or draft_type != draft_type:   # None or NaN
        return None
    text = str(draft_type).strip().lower()
    if not text:
        return None
    for token, kind in _RULES:
        if token in text:
            return kind
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or None


def ensure_draft_kind(con) -> int:
    """Give an existing database the draft_kind column, idempotently.

    The loader writes the column on every fresh build; this is the
    migration for a database built before it existed, run wherever the
    pipeline already holds a read-write connection (afl/link_draft.py).
    Returns the number of rows classified. A database without a draft
    table is left alone.
    """
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='draft'").fetchone():
        return 0
    present = {row[1] for row in con.execute("PRAGMA table_info(draft)")}
    if "draft_kind" not in present:
        con.execute("ALTER TABLE draft ADD COLUMN draft_kind TEXT")
    classified = 0
    for (value,) in con.execute("SELECT DISTINCT draft_type FROM draft"):
        classified += con.execute(
            "UPDATE draft SET draft_kind = ? WHERE draft_type IS ?",
            (draft_kind(value), value)).rowcount
    con.execute("CREATE INDEX IF NOT EXISTS ix_draft_kind "
                "ON draft(draft_kind, pick)")
    con.commit()
    return classified
