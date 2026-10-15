"""JSON_TYPE names and TO_JSON_STRING encodings over DuckDB's JSON (ADR 0009, 0014).

JSON_TYPE: BigQuery's JSON type names over DuckDB's json_type. Slice 2.

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
    for node in reversed(list(tree.find_all(exp.JSONFormat))):
        _encodable(node)
    return tree


# TO_JSON_STRING / TO_JSON leaves whose DuckDB rendering is BigQuery's: numbers and booleans as
# JSON literals, strings escaped, DATE as "YYYY-MM-DD", JSON as is (corpus json/to_json_string*).
_SAME_JSON = {
    exp.DType.BIGINT,
    exp.DType.DOUBLE,
    exp.DType.BOOLEAN,
    exp.DType.TEXT,
    exp.DType.VARCHAR,
    exp.DType.JSON,
    exp.DType.DATE,
    exp.DType.NULL,
}


def _encodable(node: exp.JSONFormat) -> None:
    """Rewrite the operand so DuckDB's JSON is BigQuery's, or raise for types whose encoding is
    not confirmed (DuckDB writes NUMERIC with trailing zeros, BYTES raw, DATETIME with a space).
    TIMESTAMP is "YYYY-MM-DDTHH:MM:SSZ" (BigQuery JSON encodings; corpus case
    json/to_json_string_timestamp); a fractional second is unsupported until recorded.
    Unrecorded values raise at run time: a NaN or infinite FLOAT64 anywhere in the value, and a
    NULL operand (DuckDB answers SQL NULL; BigQuery's answer is not recorded)."""
    value = node.this
    if not isinstance(value, exp.Expr) or value.type is None:
        raise CrucibleUnsupportedError("TO_JSON_STRING of a value of unknown type")
    encoded = _encode(value.copy(), value.type) if _needs_rewrite(value.type) else value.copy()
    value.replace(
        exp.Case(
            ifs=[
                exp.If(
                    this=exp.Is(this=value.copy(), expression=exp.Null()),
                    true=_unsupported("TO_JSON_STRING of NULL"),
                )
            ],
            default=encoded,
        )
    )


def _unsupported(construct: str) -> exp.Expr:
    return exp.Anonymous(
        this="error", expressions=[exp.Literal.string(UNSUPPORTED_ERROR_PREFIX + construct)]
    )


def _needs_rewrite(data_type: exp.DataType) -> bool:
    """Whether a TIMESTAMP (re-encoded) or FLOAT64 (checked for NaN/Inf) is inside."""
    kind = data_type.this
    if kind in (exp.DType.ARRAY, exp.DType.STRUCT):
        inner = [
            f.args["kind"] if isinstance(f, exp.ColumnDef) else f for f in data_type.expressions
        ]
        return any(_needs_rewrite(t) for t in inner if isinstance(t, exp.DataType))
    if kind in (exp.DType.TIMESTAMPTZ, exp.DType.DOUBLE):
        return True
    if kind not in _SAME_JSON:
        raise CrucibleUnsupportedError(f"TO_JSON_STRING of a {data_type.sql('bigquery')} value")
    return False


def _encode(value: exp.Expr, data_type: exp.DataType) -> exp.Expr:
    """`value` with every TIMESTAMP inside it replaced by BigQuery's JSON text for it, and every
    FLOAT64 checked for NaN and infinities."""
    kind = data_type.this
    if kind == exp.DType.DOUBLE:
        special = exp.Or(
            this=exp.Anonymous(this="isnan", expressions=[value.copy()]),
            expression=exp.Anonymous(this="isinf", expressions=[value.copy()]),
        )
        return exp.Case(
            ifs=[exp.If(this=special, true=_unsupported("TO_JSON_STRING of NaN or Infinity"))],
            default=value,
        )
    if kind == exp.DType.TIMESTAMPTZ:
        # Typed explicitly: a field of a NULL struct literal reaches DuckDB untyped.
        value = exp.Cast(this=value, to=exp.DataType.build("TIMESTAMPTZ", dialect="duckdb"))
        whole = exp.EQ(
            this=exp.Anonymous(
                this="date_trunc", expressions=[exp.Literal.string("second"), value.copy()]
            ),
            expression=value.copy(),
        )
        formatted = exp.Anonymous(
            this="strftime",
            expressions=[
                exp.Cast(this=value.copy(), to=exp.DataType.build("TIMESTAMP")),
                exp.Literal.string("%Y-%m-%dT%H:%M:%SZ"),
            ],
        )
        fractional = _unsupported("TO_JSON_STRING of a TIMESTAMP with fractional seconds")
        return exp.Case(
            ifs=[
                exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=exp.Null()),
                exp.If(this=whole, true=formatted),
            ],
            default=fractional,
        )
    if kind == exp.DType.ARRAY:
        [element] = data_type.expressions
        assert isinstance(element, exp.DataType)
        if not _needs_rewrite(element):
            return value
        var = exp.to_identifier("crucible_e")
        body = _encode(exp.column(var), element)
        return exp.Anonymous(
            this="list_transform",
            expressions=[value, exp.Lambda(this=body, expressions=[var])],
        )
    if kind == exp.DType.STRUCT:
        fields = [f for f in data_type.expressions if isinstance(f, exp.ColumnDef)]
        packed = exp.Anonymous(
            this="struct_pack",
            expressions=[
                exp.PropertyEQ(
                    this=exp.to_identifier(f.name, quoted=True),
                    expression=_encode(
                        exp.Anonymous(
                            this="struct_extract",
                            expressions=[value.copy(), exp.Literal.string(f.name)],
                        ),
                        f.args["kind"],
                    ),
                )
                for f in fields
            ],
        )
        # A NULL struct stays NULL rather than becoming a struct of NULL fields.
        return exp.Case(
            ifs=[exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=exp.Null())],
            default=packed,
        )
    return value


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
