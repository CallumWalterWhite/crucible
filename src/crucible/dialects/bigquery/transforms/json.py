"""JSON_TYPE: BigQuery's JSON type names over DuckDB's json_type (ADR 0009, 0014). Slice 2.

BigQuery answers `object`, `array`, `string`, `number`, `boolean`, `null`. DuckDB's json_type
answers `OBJECT`, `ARRAY`, `VARCHAR`, `BIGINT`, `UBIGINT`, `DOUBLE`, `BOOLEAN`, `NULL` (verified on
DuckDB 1.5.6; a JSON null is the text 'NULL', an SQL NULL is NULL). Any other name raises at run
time rather than being reported under a guessed name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import text, typed
from crucible.engine import CrucibleUnsupportedError
from crucible.engine.errors import UNSUPPORTED_ERROR_PREFIX

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

_NAMES = {
    "OBJECT": "object",
    "ARRAY": "array",
    "VARCHAR": "string",
    "BOOLEAN": "boolean",
    "NULL": "null",
    "BIGINT": "number",
    "UBIGINT": "number",
    "DOUBLE": "number",
}


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    # Innermost first, so a JSON_TYPE inside another rewrite's operand is rewritten too.
    for node in reversed(list(tree.find_all(exp.JSONType))):
        node.replace(_json_type(node))
    return tree


def _json_type(node: exp.JSONType) -> exp.Expr:
    value = node.this
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError("JSON_TYPE without a value")

    def kind() -> exp.Expr:
        return exp.Anonymous(this="json_type", expressions=[value.copy()])

    # A value DuckDB reports under a name we do not map raises, naming that name.
    unknown = exp.Anonymous(
        this="error",
        expressions=[text(UNSUPPORTED_ERROR_PREFIX, "JSON_TYPE of DuckDB JSON type ", kind())],
    )
    names = exp.Case(
        this=kind(),
        ifs=[
            exp.If(this=exp.Literal.string(duckdb), true=exp.Literal.string(bigquery))
            for duckdb, bigquery in _NAMES.items()
        ],
        default=unknown,
    )
    # SQL NULL in, NULL out, checked before json_type sees it.
    return typed(
        exp.Case(
            ifs=[exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=exp.Null())],
            default=names,
        ),
        "STRING",
    )
