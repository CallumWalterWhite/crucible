"""Arrays: ARRAY_CONCAT (and `||` on arrays, which translate.py rewrites to it).

BigQuery's ARRAY_CONCAT "returns NULL if any input argument is NULL" (BigQuery reference, array
functions). DuckDB's list_concat skips a NULL list instead, so `ARRAY_CONCAT([1], NULL)` would be
[1]. The call is wrapped in CASE WHEN <any argument> IS NULL THEN NULL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import VOLATILE, check, rewrite_bottom_up
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    return rewrite_bottom_up(tree, _rewrite)


def _rewrite(node: exp.Expr) -> exp.Expr:
    if not isinstance(node, exp.ArrayConcat):
        return node
    arrays = [node.this, *node.expressions]
    if any(VOLATILE.search(a.sql("bigquery")) for a in arrays):
        # The check evaluates each argument a second time; a volatile one could differ.
        raise CrucibleUnsupportedError("ARRAY_CONCAT of a non-deterministic array")
    nulls = [exp.Is(this=a.copy(), expression=exp.Null()) for a in arrays]
    any_null = nulls[0]
    for null in nulls[1:]:
        any_null = exp.Or(this=any_null, expression=null)
    empty = exp.Null()
    empty.type = node.type
    return check(any_null, empty, node)
