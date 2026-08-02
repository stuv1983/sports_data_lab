"""Family-draft extension for the existing Advanced Search compiler.

This module wraps ``query_filters`` rather than forking it.  Existing search
syntax, URL parameters and result specification remain owned by the base
compiler; only family-draft tokens are removed, compiled to parameterised SQL
and added to the final player predicate.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

import query_filters as _base
from query_filters import *  # noqa: F401,F403 - preserve the public API

import family_draft as F

QuerySyntaxError = _base.QuerySyntaxError

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_FAMILY_KEYS = {
    "father_son", "family_draft", "father_played", "father_club",
    "father", "child_of", "parent_child", "family_pair",
}


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


def _quote(value: object) -> str:
    return shlex.quote(str(value))


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
    try:
        tokens = shlex.split(query, posix=True)
    except ValueError as exc:
        raise QuerySyntaxError(str(exc)) from exc

    for token in tokens:
        if ":" not in token:
            base_tokens.append(token)
            continue
        key, value = token.split(":", 1)
        key = key.strip().casefold()
        if key in _FAMILY_KEYS:
            family_tokens.append((key, value.strip()))
        else:
            base_tokens.append(token)
    return base_tokens, family_tokens


def _family_constraints(tokens: list[tuple[str, str]]):
    constraints = []
    descriptions = []
    for key, value in tokens:
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
            if not value:
                raise QuerySyntaxError("father_club requires a club")
            sql, params = F.father_played_for(value)
            description = f"father played for {value}"
        elif key in {"father", "child_of"}:
            if not value:
                raise QuerySyntaxError(f"{key} requires a father name")
            sql, params = F.child_of_father_name(value)
            description = f"child of {value}"
        elif key in {"parent_child", "family_pair"}:
            enabled = _bool(value, key)
            sql, params = F.parent_child_pair()
            negate = not enabled
            description = "member of a linked parent-child pair" if enabled else "not in a linked parent-child pair"
        else:  # defensive; _split_query already whitelists
            raise QuerySyntaxError(f"unknown family filter: {key}")
        constraints.append((sql, list(params), negate))
        descriptions.append(description)
    return constraints, descriptions


def _inject(sql: str, params: list, schema, constraints):
    if not constraints:
        return sql, params

    order = re.search(r"\bORDER\s+BY\b", sql, flags=re.I)
    if not order:
        raise QuerySyntaxError("base search SQL has no ORDER BY insertion point")
    head, tail = sql[:order.start()].rstrip(), sql[order.start():]
    insertion_param_index = head.count("?")

    clauses = []
    family_params = []
    for fragment, values, negate in constraints:
        operator = "NOT IN" if negate else "IN"
        clauses.append(f"p.{schema.player_id} {operator} ({fragment})")
        family_params.extend(values)

    joiner = "\n  AND " if re.search(r"\bWHERE\b", head, flags=re.I) else "\nWHERE "
    new_sql = head + joiner + "\n  AND ".join(clauses) + "\n" + tail
    new_params = (
        list(params[:insertion_param_index])
        + family_params
        + list(params[insertion_param_index:])
    )
    return new_sql, new_params


def compile_query(schema, query, con=None):
    base_tokens, family_tokens = _split_query(query)
    if family_tokens:
        if con is not None:
            F.ensure_family_draft_table(con)
            if not F.family_draft_available(con):
                raise QuerySyntaxError(
                    "family-draft data is not loaded; run load_family_draft.py"
                )
        constraints, descriptions = _family_constraints(family_tokens)
    else:
        constraints, descriptions = [], []

    # The base compiler's normal default ordering is obscurity.  Supplying an
    # explicit sort token also gives family-only searches a non-empty base
    # expression without changing their result semantics.
    base_query = shlex.join(base_tokens) if base_tokens else "sort:obscurity"
    sql, params, spec = _base.compile_query(schema, base_query, con=con)
    sql, params = _inject(sql, list(params), schema, constraints)
    return sql, params, FamilySearchSpec(spec, descriptions)


def query_from_params(params: dict) -> str:
    query = _base.query_from_params(params)
    extra: list[str] = []
    mappings = (
        ("father_son", "father_son"),
        ("family_draft", "family_draft"),
        ("father_played", "father_played"),
        ("parent_child", "parent_child"),
        ("family_pair", "family_pair"),
        ("father_club", "father_club"),
        ("father", "father"),
        ("child_of", "child_of"),
    )
    for param_name, token_name in mappings:
        for value in _values(params, param_name):
            extra.append(f"{token_name}:{_quote(value)}")
    return " ".join(part for part in (query.strip(), " ".join(extra)) if part)


def describe(spec) -> list[str]:
    if isinstance(spec, FamilySearchSpec):
        return list(_base.describe(spec.base)) + list(spec.family_descriptions)
    return list(_base.describe(spec))
