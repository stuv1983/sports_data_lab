"""Family extensions for the Sports Data Lab Advanced Search compiler.

This module wraps :mod:`query_filters` so the sport-agnostic compiler remains
unchanged. It supports both the narrow father-son draft layer and the broad
Wikipedia football-family relationship layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import query_filters as _base
from query_filters import *  # noqa: F401,F403 - preserve public API

QuerySyntaxError = _base.QuerySyntaxError

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_DRAFT_KEYS = {
    "father_son", "family_draft", "father_played", "father_club",
    "father", "child_of", "parent_child", "family_pair",
}
_BROAD_KEYS = {"family", "family_relation", "related_to", "relative_club"}
_TRUSTED = "('unique','resolved')"


@dataclass
class FamilySearchSpec:
    """Proxy retaining every attribute of the base search specification."""

    base: Any
    family_descriptions: list[str]

    def __getattr__(self, name: str):
        return getattr(self.base, name)


def _bool(value: str, field: str) -> bool:
    normal = value.strip().casefold()
    if normal in _TRUE:
        return True
    if normal in _FALSE:
        return False
    raise QuerySyntaxError(f"{field} expects true or false")


def _table_exists(con, name: str) -> bool:
    if con is None:
        return True
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _values(params: dict, key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _split_query(query: str):
    base_tokens: list[str] = []
    family_tokens: list[tuple[str, str]] = []
    for token in _base.tokenize(query):
        match = _base._TOKEN.fullmatch(token)
        if not match:
            base_tokens.append(token)
            continue
        key, operator, value = match.groups()
        key = key.strip().casefold().replace("-", "_")
        if key in _DRAFT_KEYS | _BROAD_KEYS:
            if operator not in {":", "="}:
                raise QuerySyntaxError(f"{key} supports only field:value syntax")
            if not value:
                raise QuerySyntaxError(f"{key} needs a value")
            family_tokens.append((key, value.strip()))
        else:
            base_tokens.append(token)
    return base_tokens, family_tokens


def _draft_constraint(key: str, value: str, con):
    try:
        from afl import family_draft as F
    except ImportError as exc:
        raise QuerySyntaxError("family-draft support is not installed") from exc

    if con is not None:
        F.ensure_family_draft_table(con)
        if not F.family_draft_available(con):
            raise QuerySyntaxError(
                "family-draft data is not loaded; run afl/load_family_draft.py"
            )

    negate = False
    if key in {"father_son", "family_draft"}:
        enabled = _bool(value, key)
        sql, params = F.father_son_selection()
        negate = not enabled
        description = "father-son selection" if enabled else "not a father-son selection"
    elif key == "father_played":
        enabled = _bool(value, key)
        sql, params = F.father_also_played_afl()
        negate = not enabled
        description = "father also played AFL" if enabled else "father not linked as an AFL player"
    elif key == "father_club":
        sql, params = F.father_played_for(value)
        description = f"father played for {value}"
    elif key in {"father", "child_of"}:
        sql, params = F.child_of_father_name(value)
        description = f"child of {value}"
    elif key in {"parent_child", "family_pair"}:
        enabled = _bool(value, key)
        sql, params = F.parent_child_pair()
        negate = not enabled
        description = "member of a linked parent-child pair" if enabled else "not in a linked parent-child pair"
    else:
        raise QuerySyntaxError(f"unknown family-draft filter: {key}")
    return sql, list(params), negate, description


def _broad_constraint(schema, key: str, value: str, con):
    required = ["family_members"]
    if key == "family_relation":
        required.append("family_relationships")
    if con is not None and any(not _table_exists(con, table) for table in required):
        raise QuerySyntaxError(
            "family relationship data is not loaded; run "
            "afl/scrape_wikipedia_families.py then afl/load_family_relationships.py"
        )

    s = schema
    if key == "family":
        wanted = _bool(value, key)
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
        return sql, [], not wanted, "football family member"

    if key == "family_relation":
        relation = value.strip().lower().replace("-", "_")
        relation_where = {
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
        }.get(relation)
        if relation_where is None:
            raise QuerySyntaxError(
                "family_relation must be sibling, brother, parent_child, "
                "father_son, extended or spouse"
            )
        sql = (
            "SELECT fa.player_id FROM family_relationships r "
            "JOIN family_members fa ON fa.source_member_id=r.person_a_source_member_id "
            "JOIN family_members fb ON fb.source_member_id=r.person_b_source_member_id "
            "WHERE fa.player_id IS NOT NULL AND fb.player_id IS NOT NULL "
            f"AND fa.match_status IN {_TRUSTED} AND fb.match_status IN {_TRUSTED} "
            f"AND ({relation_where}) UNION SELECT fb.player_id FROM family_relationships r "
            "JOIN family_members fa ON fa.source_member_id=r.person_a_source_member_id "
            "JOIN family_members fb ON fb.source_member_id=r.person_b_source_member_id "
            "WHERE fa.player_id IS NOT NULL AND fb.player_id IS NOT NULL "
            f"AND fa.match_status IN {_TRUSTED} AND fb.match_status IN {_TRUSTED} "
            f"AND ({relation_where})"
        )
        return sql, [], False, f"family relation: {relation}"

    if key == "related_to":
        sql = (
            "SELECT DISTINCT self.player_id FROM family_members self "
            "JOIN family_members relative ON relative.family_key=self.family_key "
            "AND relative.source_member_id<>self.source_member_id "
            f"JOIN {s.players} rp ON rp.{s.player_id}=relative.player_id "
            "WHERE self.player_id IS NOT NULL "
            f"AND self.match_status IN {_TRUSTED} "
            f"AND relative.match_status IN {_TRUSTED} "
            f"AND LOWER(rp.{s.player}) LIKE ?"
        )
        return sql, [f"%{value.lower()}%"], False, f"related to {value}"

    if key == "relative_club":
        sql = (
            "SELECT DISTINCT self.player_id FROM family_members self "
            "JOIN family_members relative ON relative.family_key=self.family_key "
            "AND relative.source_member_id<>self.source_member_id "
            f"JOIN {s.games} rg ON rg.{s.player_id}=relative.player_id "
            "WHERE self.player_id IS NOT NULL "
            f"AND self.match_status IN {_TRUSTED} "
            f"AND relative.match_status IN {_TRUSTED} "
            f"AND (LOWER(rg.{s.club_now})=LOWER(?) "
            f"OR LOWER(rg.{s.club_hist})=LOWER(?))"
        )
        return sql, [value, value], False, f"relative played for {value}"

    raise QuerySyntaxError(f"unknown family filter: {key}")


def _inject(sql: str, params: list, schema, constraints):
    if not constraints:
        return sql, params
    order = re.search(r"\bORDER\s+BY\b", sql, flags=re.I)
    if not order:
        raise QuerySyntaxError("base search SQL has no ORDER BY insertion point")
    head, tail = sql[:order.start()].rstrip(), sql[order.start():]
    insertion_param_index = head.count("?")
    clauses, family_params = [], []
    for fragment, values, negate in constraints:
        clauses.append(
            f"p.{schema.player_id} {'NOT IN' if negate else 'IN'} ({fragment})"
        )
        family_params.extend(values)
    new_sql = head + "\n  AND " + "\n  AND ".join(clauses) + "\n" + tail
    new_params = (
        list(params[:insertion_param_index])
        + family_params
        + list(params[insertion_param_index:])
    )
    return new_sql, new_params


def compile_query(schema, query, con=None):
    base_tokens, family_tokens = _split_query(query)
    constraints, descriptions = [], []
    for key, value in family_tokens:
        if key in _DRAFT_KEYS:
            sql, params, negate, description = _draft_constraint(key, value, con)
        else:
            sql, params, negate, description = _broad_constraint(
                schema, key, value, con
            )
        constraints.append((sql, params, negate))
        descriptions.append(description)

    base_query = (_base.join_tokens(base_tokens) if base_tokens
                  else "sort:obscurity")
    sql, params, spec = _base.compile_query(schema, base_query, con=con)
    sql, params = _inject(sql, list(params), schema, constraints)
    return sql, params, FamilySearchSpec(spec, descriptions)


def query_from_params(params: dict) -> str:
    query = _base.query_from_params(params)
    extra: list[str] = []
    for key in sorted(_DRAFT_KEYS | _BROAD_KEYS):
        for value in _values(params, key):
            extra.append(f"{key}:{_base.quote_token(value)}")
    return " ".join(part for part in (query.strip(), " ".join(extra)) if part)


def describe(spec) -> list[str]:
    if isinstance(spec, FamilySearchSpec):
        return list(_base.describe(spec.base)) + list(spec.family_descriptions)
    return list(_base.describe(spec))
