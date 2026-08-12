"""
afl/search_tokens.py -- The AFL's Advanced Search token extensions.

Captaincy, Draftguru draft/award links and the Wikipedia family layers are
AFL data shapes, so the tokens that search them live here and are declared
by ``search_extension_modules`` on the AFL's Sport entry -- the same way
BUILDERS declares its grid constraints. The shared compiler in
query_filters.py knows none of these tables; it offers unrecognised tokens
to each extension's ``claim`` and appends whatever ``finish`` returns.

A second sport that grows a draft or captaincy layer with a different
shape writes its own module like this one and declares it; nothing in the
framework changes.
"""

from __future__ import annotations

import names
from query_filters import (
    QuerySyntaxError,
    SearchExtension,
    _boolean,
    _club_identities,
    _field_only,
    _range,
    _table_exists,
)

#: The link statuses a search may trust, matching the grid engine's rule:
#: an ambiguous link must not answer a search any more than it answers a
#: grid square.
_TRUSTED = "('unique','resolved')"

#: Trusted draft rows joined to the player they resolved to, shared by
#: every draft token so they agree on which links count.
_DRAFTED = ("SELECT dl.player_id FROM draft d JOIN draft_links dl "
            "ON dl.draft_rowid = d.rowid "
            "WHERE dl.match_status IN ('unique','resolved') "
            "AND dl.player_id IS NOT NULL")


def _require_draft(con) -> None:
    if not _table_exists(con, "draft") or not _table_exists(con, "draft_links"):
        raise QuerySyntaxError("Draft data is not loaded")


class CaptainTokens(SearchExtension):
    """`captain:`, `captain_club:` and `captain_year:`/`captain_season:`.

    The positive conditions accumulate into ONE captaincies subquery, so
    `captain_club:Carlton captain_year:1995..2001` means "captained
    Carlton in those seasons" -- the same row -- rather than two
    independent facts that happen to share a player.
    """

    def __init__(self):
        self.conditions: list[str] = []
        self.params: list = []
        self.negated = False

    def claim(self, key, operator, value):
        if key == "captain":
            _field_only(key, operator)
            if _boolean(value):
                self.conditions.append("1=1")
            else:
                self.negated = True
            return True
        if key == "captain_club":
            _field_only(key, operator)
            # COLLATE NOCASE keeps the stored column bare -- the old
            # LOWER(cp.club)=LOWER(?) wrapped the indexed side -- and can
            # ride a NOCASE index if the captaincies loader ever adds one.
            self.conditions.append("cp.club COLLATE NOCASE = ?")
            self.params.append(value)
            return True
        if key in {"captain_year", "captain_season"}:
            _field_only(key, operator)
            lo, hi = _range(value, key)
            self.conditions.append("cp.season BETWEEN ? AND ?")
            self.params.extend([lo, hi])
            return True
        return False

    def finish(self, schema, con):
        if not self.conditions and not self.negated:
            return []
        if not _table_exists(con, "captaincies"):
            raise QuerySyntaxError("Captaincy data is not loaded")
        out = []
        if self.negated:
            out.append((
                f"p.{schema.player_id} NOT IN "
                "(SELECT cp.player_id FROM captaincies cp "
                f"WHERE cp.match_status IN {_TRUSTED})", []))
        if self.conditions:
            out.append((
                f"p.{schema.player_id} IN "
                "(SELECT cp.player_id FROM captaincies cp "
                f"WHERE cp.match_status IN {_TRUSTED} AND "
                + " AND ".join(self.conditions) + ")", self.params))
        return out


class AwardTokens(SearchExtension):
    """`award:<slug>` against the Draftguru awards + person_links layer."""

    def __init__(self):
        self.slugs: list[str] = []

    def claim(self, key, operator, value):
        if key != "award":
            return False
        _field_only(key, operator)
        self.slugs.append(value)
        return True

    def finish(self, schema, con):
        if not self.slugs:
            return []
        if not _table_exists(con, "awards") or not _table_exists(con, "person_links"):
            raise QuerySyntaxError("Award data is not loaded")
        return [(
            f"p.{schema.player_id} IN "
            "(SELECT al.player_id FROM awards a JOIN person_links al "
            "ON al.dg_person_id=a.dg_person_id "
            "WHERE al.match_status IN ('from_draft','unique','resolved') "
            "AND a.award_slug=?)", [slug])
            for slug in self.slugs]


class DraftTokens(SearchExtension):
    """Draftguru draft tokens: recruited_from, pick, draft_year, drafted_by."""

    def __init__(self):
        self.tokens: list[tuple] = []

    def claim(self, key, operator, value):
        if key in {"recruited_from", "recruited", "from_club"}:
            _field_only(key, operator)
            self.tokens.append(("recruited", value))
            return True
        if key in {"pick", "draft_pick"}:
            _field_only(key, operator)
            self.tokens.append(("pick", _range(value, key)))
            return True
        if key in {"draft_year", "drafted_year"}:
            _field_only(key, operator)
            self.tokens.append(("year", _range(value, key)))
            return True
        if key == "drafted_by":
            _field_only(key, operator)
            self.tokens.append(("by", value))
            return True
        return False

    def finish(self, schema, con):
        if not self.tokens:
            return []
        _require_draft(con)
        from afl import recruitment

        out = []
        for kind, value in self.tokens:
            if kind == "recruited":
                # `original_club` is a path -- "Greythorn / Xavier College /
                # Oakleigh U18" -- so the term matches a whole step of it.
                out.append((
                    f"p.{schema.player_id} IN ({_DRAFTED} AND "
                    f"{recruitment.segment_or_prefix_sql('d.original_club')})",
                    [value, value]))
            elif kind == "pick":
                # National draft only: Draftguru restarts pick numbering
                # for the rookie, pre-season and mid-season drafts, so an
                # unqualified pick:1..10 sweeps in four different pick 1s.
                # draft_kind is the ingestion-assigned category -- a bare
                # indexed equality, where LIKE over the raw label scanned.
                lo, hi = value
                out.append((
                    f"p.{schema.player_id} IN ({_DRAFTED} "
                    "AND d.draft_kind = 'national' "
                    "AND d.pick BETWEEN ? AND ?)", [lo, hi]))
            elif kind == "year":
                lo, hi = value
                out.append((
                    f"p.{schema.player_id} IN ({_DRAFTED} "
                    "AND d.draft_year BETWEEN ? AND ?)", [lo, hi]))
            else:
                out.append((
                    f"p.{schema.player_id} IN "
                    "(SELECT dl.player_id FROM draft d JOIN draft_links dl "
                    "ON dl.draft_rowid=d.rowid "
                    f"WHERE dl.match_status IN {_TRUSTED} "
                    "AND LOWER(d.club) LIKE ? ESCAPE '\\')",
                    [names.like_contains(str(value).lower())]))
        return out


class FamilyDraftTokens(SearchExtension):
    """The narrow father-son draft layer: father_son:, father_club:, etc."""

    KEYS = {"father_son", "family_draft", "father_played", "father_club",
            "father", "child_of", "parent_child", "family_pair"}

    def __init__(self):
        self.tokens: list[tuple] = []

    def claim(self, key, operator, value):
        if key not in self.KEYS:
            return False
        _field_only(key, operator)
        self.tokens.append((key, value.strip()))
        return True

    def finish(self, schema, con):
        if not self.tokens:
            return []
        try:
            from afl import family_draft as F
        except ImportError as exc:
            raise QuerySyntaxError(
                "family-draft support is not installed") from exc
        if con is not None:
            F.ensure_family_draft_table(con)
            if not F.family_draft_available(con):
                raise QuerySyntaxError(
                    "family-draft data is not loaded; "
                    "run afl/load_family_draft.py")

        out = []
        for key, value in self.tokens:
            negate = False
            if key in {"father_son", "family_draft"}:
                sql, params = F.father_son_selection()
                negate = not _boolean(value)
            elif key == "father_played":
                sql, params = F.father_also_played_afl()
                negate = not _boolean(value)
            elif key == "father_club":
                sql, params = F.father_played_for(value)
            elif key in {"father", "child_of"}:
                sql, params = F.child_of_father_name(value)
            else:                        # parent_child / family_pair
                sql, params = F.parent_child_pair()
                negate = not _boolean(value)
            membership = "NOT IN" if negate else "IN"
            out.append((f"p.{schema.player_id} {membership} ({sql})",
                        list(params)))
        return out


class FamilyTokens(SearchExtension):
    """The broad Wikipedia football-family layer: family:, family_relation:,
    related_to: and relative_club:."""

    KEYS = {"family", "family_relation", "related_to", "relative_club"}

    #: family_relation value -> predicate over one family_relationships row.
    RELATIONS = {
        "sibling": "r.relationship_type='sibling'",
        "brother": (
            "r.relationship_type='sibling' AND ("
            "LOWER(COALESCE(r.relationship_label,'')) LIKE '%brother%' "
            "OR LOWER(COALESCE(r.relationship_label,''))='twins' "
            "OR LOWER(COALESCE(r.person_a_role,''))='brother' "
            "OR LOWER(COALESCE(r.person_b_role,''))='brother')"
        ),
        "parent_child": "r.relationship_type='parent_child'",
        "father_son": (
            "r.relationship_type='parent_child' AND ("
            "LOWER(COALESCE(r.relationship_label,'')) LIKE '%father%' "
            "OR LOWER(COALESCE(r.relationship_label,'')) LIKE '%son%' "
            "OR LOWER(COALESCE(r.person_a_role,'')) IN ('father','son') "
            "OR LOWER(COALESCE(r.person_b_role,'')) IN ('father','son'))"
        ),
        "extended": (
            "r.relationship_type IN ("
            "'grandparent_grandchild','aunt_uncle_niece_nephew',"
            "'cousin','in_law')"
        ),
        "spouse": "r.relationship_type='spouse'",
    }

    def __init__(self):
        self.tokens: list[tuple] = []

    def claim(self, key, operator, value):
        if key not in self.KEYS:
            return False
        _field_only(key, operator)
        self.tokens.append((key, value.strip()))
        return True

    def _require_tables(self, con, key):
        required = ["family_members"]
        if key == "family_relation":
            required.append("family_relationships")
        if con is not None and any(not _table_exists(con, table)
                                   for table in required):
            raise QuerySyntaxError(
                "family relationship data is not loaded; run "
                "afl/scrape_wikipedia_families.py then "
                "afl/load_family_relationships.py")

    def finish(self, schema, con):
        out = []
        s = schema
        for key, value in self.tokens:
            self._require_tables(con, key)
            if key == "family":
                wanted = _boolean(value)
                sql = (
                    "SELECT DISTINCT fm.player_id FROM family_members fm "
                    "WHERE fm.player_id IS NOT NULL "
                    f"AND fm.match_status IN {_TRUSTED} "
                    "AND EXISTS (SELECT 1 FROM family_members fr "
                    "WHERE fr.family_key=fm.family_key "
                    "AND fr.source_member_id<>fm.source_member_id "
                    "AND fr.player_id IS NOT NULL "
                    f"AND fr.match_status IN {_TRUSTED})"
                )
                membership = "IN" if wanted else "NOT IN"
                out.append((f"p.{s.player_id} {membership} ({sql})", []))
            elif key == "family_relation":
                relation = value.lower().replace("-", "_")
                predicate = self.RELATIONS.get(relation)
                if predicate is None:
                    raise QuerySyntaxError(
                        "family_relation must be sibling, brother, "
                        "parent_child, father_son, extended or spouse")
                # Both ends of every matching relationship row: the UNION
                # returns person A's and person B's player ids alike.
                arm = (
                    "SELECT f{side}.player_id FROM family_relationships r "
                    "JOIN family_members fa "
                    "ON fa.source_member_id=r.person_a_source_member_id "
                    "JOIN family_members fb "
                    "ON fb.source_member_id=r.person_b_source_member_id "
                    "WHERE fa.player_id IS NOT NULL "
                    "AND fb.player_id IS NOT NULL "
                    f"AND fa.match_status IN {_TRUSTED} "
                    f"AND fb.match_status IN {_TRUSTED} "
                    f"AND ({predicate})"
                )
                sql = arm.format(side="a") + " UNION " + arm.format(side="b")
                out.append((f"p.{s.player_id} IN ({sql})", []))
            elif key == "related_to":
                sql = (
                    "SELECT DISTINCT self.player_id FROM family_members self "
                    "JOIN family_members relative "
                    "ON relative.family_key=self.family_key "
                    "AND relative.source_member_id<>self.source_member_id "
                    f"JOIN {s.players} rp "
                    f"ON rp.{s.player_id}=relative.player_id "
                    "WHERE self.player_id IS NOT NULL "
                    f"AND self.match_status IN {_TRUSTED} "
                    f"AND relative.match_status IN {_TRUSTED} "
                    f"AND LOWER(rp.{s.player}) LIKE ?"
                )
                out.append((f"p.{s.player_id} IN ({sql})",
                            [f"%{value.lower()}%"]))
            else:                        # relative_club
                # Same club-identity rule as the base compiler's club:
                # aliases and era names resolve in Python to an exact,
                # indexable IN; an unknown name is an error, not a
                # LOWER()-wrapped scan of the games table.
                identities = _club_identities(s, value)
                if not identities:
                    raise QuerySyntaxError(f"Unknown club: {value!r}")
                marks = ",".join("?" for _ in identities)
                sql = (
                    "SELECT DISTINCT self.player_id FROM family_members self "
                    "JOIN family_members relative "
                    "ON relative.family_key=self.family_key "
                    "AND relative.source_member_id<>self.source_member_id "
                    f"JOIN {s.games} rg "
                    f"ON rg.{s.player_id}=relative.player_id "
                    "WHERE self.player_id IS NOT NULL "
                    f"AND self.match_status IN {_TRUSTED} "
                    f"AND relative.match_status IN {_TRUSTED} "
                    f"AND (rg.{s.club_now} IN ({marks}) "
                    f"OR rg.{s.club_hist} IN ({marks}))"
                )
                out.append((f"p.{s.player_id} IN ({sql})",
                            [*identities, *identities]))
        return out


def extensions() -> list:
    """Fresh extension instances for one compile. Registered on the Sport."""
    return [CaptainTokens(), AwardTokens(), DraftTokens(),
            FamilyDraftTokens(), FamilyTokens()]
