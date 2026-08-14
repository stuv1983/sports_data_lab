"""Compatibility façade over the Advanced Search compiler.

The family, captaincy and draft tokens this module used to compile inline
now live in afl/search_tokens.py as :class:`query_filters.SearchExtension`
subclasses, declared by ``search_extension_modules`` on the AFL's Sport
entry -- the shared compiler carries no sport's tables. What remains here
is the public surface existing callers import:

* every name from :mod:`query_filters`, re-exported unchanged;
* :func:`compile_query`, forwarding ``extensions`` through (callers pass
  ``sport.search_extensions()``);
* :func:`query_from_params`, which still translates the family URL
  parameters into query tokens;
* :func:`describe`, which still reads the older FamilySearchSpec shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import query_filters as _base
from query_filters import *  # noqa: F401,F403 - preserve public API

QuerySyntaxError = _base.QuerySyntaxError

#: URL parameter names the family layers answer to, kept for deep links.
_DRAFT_KEYS = {
    "father_son", "family_draft", "father_played", "father_club",
    "father", "child_of", "parent_child", "family_pair",
}
_BROAD_KEYS = {"family", "family_relation", "related_to", "relative_club"}


@dataclass
class FamilySearchSpec:
    """Older spec shape that carried family descriptions alongside the
    base QuerySpec. compile_query no longer produces it -- extension
    tokens ride in QuerySpec.filters like any other -- but describe()
    still reads one, so a caller holding an old spec keeps working."""

    base: Any
    family_descriptions: list[str]

    def __getattr__(self, name: str):
        return getattr(self.base, name)


def _values(params: dict, key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def compile_query(schema, query, con=None, extensions=()):
    return _base.compile_query(schema, query, con=con, extensions=extensions)


def query_from_params(params: dict) -> str:
    query = _base.query_from_params(params)
    if _values(params, "q"):
        # `q=` is the canonical whole-query parameter; the base compiler
        # returns it verbatim. Appending the structured family parameters
        # after it duplicated filters (and grew the query on every
        # canonicalisation pass) whenever a URL carried both forms.
        return query.strip()
    extra: list[str] = []
    for key in sorted(_DRAFT_KEYS | _BROAD_KEYS):
        for value in _values(params, key):
            extra.append(f"{key}:{_base.quote_token(value)}")
    return " ".join(part for part in (query.strip(), " ".join(extra)) if part)


def describe(spec) -> list[str]:
    if isinstance(spec, FamilySearchSpec):
        return list(_base.describe(spec.base)) + list(spec.family_descriptions)
    return list(_base.describe(spec))
