"""Literal typing by BigQuery rules (ADR 0014, "Literals").

BigQuery types an integer literal INT64 and a literal with a fraction or exponent FLOAT64. DuckDB
types `2.5` as DECIMAL(2,1) and `1` as INTEGER, so `2147483647 + 1` overflows INT32 and `ROUND(2.5)`
returns a DECIMAL. Fractional literals are cast to DOUBLE everywhere. Integer literals are cast to
BIGINT only as operands of arithmetic, where INT32 overflow is the problem; elsewhere DuckDB adapts
an integer literal to the parameter type, and some functions (ROUND digits, REGEXP_EXTRACT group)
accept only INTEGER.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import rewrite_bottom_up

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

# Literals that are positions or syntax, not values: GROUP BY 1, ORDER BY 2, LIMIT 5,
# INTERVAL 3 DAY, NUMERIC(10, 2).
_SYNTAX_PARENTS = (
    exp.Group,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.Fetch,
    exp.Interval,
    exp.DataTypeParam,
    exp.DataType,
)
_INT64 = exp.DataType.build("BIGINT", dialect="duckdb")
_FLOAT64 = exp.DataType.build("DOUBLE", dialect="duckdb")


def _is_integer(text: str) -> bool:
    return text.isdigit() or (text.lower().startswith("0x") and len(text) > 2)


_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)


def _in_arithmetic(node: exp.Expr) -> bool:
    parent = node.parent
    while isinstance(parent, exp.Neg | exp.Paren):
        parent = parent.parent
    return isinstance(parent, _ARITHMETIC)


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    def rewrite(node: exp.Expr) -> exp.Expr:
        if not isinstance(node, exp.Literal) or node.is_string:
            return node
        # Already explicitly typed (e.g. CAST(1 AS NUMERIC)), or not a value: leave it alone.
        parent = node.parent
        if isinstance(parent, (exp.Cast, *_SYNTAX_PARENTS)):
            return node
        # DATE_ADD(d, INTERVAL 3 DAY) parses with the 3 directly under DateAdd, next to `unit`.
        if parent is not None and parent.args.get("unit") is not None:
            return node
        if _is_integer(node.this):
            if not _in_arithmetic(node):
                return node
            target = _INT64
        else:
            target = _FLOAT64
        cast = exp.Cast(this=node.copy(), to=target.copy())
        cast.type = target.copy()
        return cast

    return rewrite_bottom_up(tree, rewrite)
