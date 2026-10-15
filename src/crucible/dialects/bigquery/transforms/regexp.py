"""REGEXP_EXTRACT and REGEXP_SUBSTR: NULL when there is no match (ADR 0009, 0011). Slice 2.

BigQuery returns NULL when the pattern does not match; DuckDB's regexp_extract returns ''. An empty
match is still ''. So the call is kept, and guarded by regexp_matches (verified on DuckDB 1.5.6:
regexp_matches('abc', 'x*') is true, regexp_extract('abc', 'x*') is '').
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import rewrite_bottom_up, typed
from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

MAX_GROUPS = 1


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    return rewrite_bottom_up(tree, _rewrite)


def _rewrite(node: exp.Expr) -> exp.Expr:
    return _extract(node) if isinstance(node, exp.RegexpExtract) else node


def _extract(node: exp.RegexpExtract) -> exp.Expr:
    if node.args.get("position") is not None or node.args.get("occurrence") is not None:
        raise CrucibleUnsupportedError("REGEXP_EXTRACT with position/occurrence")
    subject, pattern = node.this, node.expression
    if not isinstance(subject, exp.Expr) or not isinstance(pattern, exp.Expr):
        raise CrucibleUnsupportedError("REGEXP_EXTRACT without a value and a pattern")
    literal = _literal_text(pattern)
    if literal is None:
        # The one-capturing-group rule needs the pattern's text, so it cannot be checked here.
        raise CrucibleUnsupportedError("REGEXP_EXTRACT with a non-literal pattern")
    _check_groups(literal)
    matched = exp.Anonymous(this="regexp_matches", expressions=[subject.copy(), pattern.copy()])
    guarded = exp.Case(
        ifs=[exp.If(this=typed(matched, "BOOL"), true=node.copy())],
        default=exp.Null(),
    )
    return typed(guarded, "STRING")


def _literal_text(pattern: exp.Expr) -> str | None:
    """The pattern's text when it is a string literal (plain or r'' raw), else None."""
    if isinstance(pattern, exp.RawString):
        return str(pattern.this)
    if isinstance(pattern, exp.Literal) and pattern.is_string:
        return str(pattern.this)
    return None


def _check_groups(pattern: str) -> None:
    try:
        groups = re.compile(pattern).groups
    except re.error as exc:
        raise CrucibleUnsupportedError(
            f"REGEXP_EXTRACT pattern Python cannot parse: {exc}"
        ) from exc
    if groups > MAX_GROUPS:
        raise CrucibleError(
            ErrorKind.INVALID_QUERY,
            "Regular expressions passed into extraction functions must not have more than "
            f"{MAX_GROUPS} capturing group",
        )
