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

from crucible.dialects.bigquery.transforms.common import numeric_text, text, typed
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


# TO_JSON_STRING / TO_JSON leaves whose DuckDB rendering is BigQuery's: integers and booleans as
# JSON literals, strings escaped, DATE as "YYYY-MM-DD", JSON as is (corpus json/to_json_string*).
_SAME_JSON = {
    exp.DType.BIGINT,
    exp.DType.BOOLEAN,
    exp.DType.TEXT,
    exp.DType.VARCHAR,
    exp.DType.JSON,
    exp.DType.DATE,
    exp.DType.NULL,
}
# Leaves re-encoded by _encode, each as recorded (corpus cases json/to_json_string_*).
_REWRITTEN = {
    exp.DType.DOUBLE,
    exp.DType.DECIMAL,
    exp.DType.BLOB,
    exp.DType.BINARY,
    exp.DType.VARBINARY,
    exp.DType.TIMESTAMP,
    exp.DType.TIMESTAMPTZ,
    exp.DType.TIME,
}
_MAX_EXACT_DOUBLE = 2**53
# Leaves _leaf writes as VARCHAR text, not JSON.
_TEXT_LEAVES = {
    exp.DType.BLOB,
    exp.DType.BINARY,
    exp.DType.VARBINARY,
    exp.DType.TIMESTAMP,
    exp.DType.TIMESTAMPTZ,
    exp.DType.TIME,
}


def _encodable(node: exp.JSONFormat) -> None:
    """Rewrite the operand so that DuckDB's JSON is BigQuery's. Encodings, all recorded:
    - TIMESTAMP "2024-03-04T05:06:07Z", DATETIME "2024-03-04T05:06:07", TIME "05:06:07"; a
      fraction is 3 digits when it is whole milliseconds (.500, .123) and 6 otherwise (.000001);
    - NUMERIC: a whole number is a JSON number (2), anything else a string without trailing
      zeros ("1.5"); a whole number beyond 2^53 is unrecorded and raises;
    - BYTES base64; FLOAT64 NaN and infinities as the strings "NaN", "Infinity", "-Infinity", and
      a whole FLOAT64 below 2^53 without a fraction (778); a whole one beyond 2^53 raises;
    - TO_JSON_STRING(NULL) is 'null' (TO_JSON(NULL) is unrecorded and raises).
    Other types (INTERVAL, ...) raise unsupported."""
    value = node.this
    if not isinstance(value, exp.Expr) or value.type is None:
        raise CrucibleUnsupportedError("TO_JSON_STRING of a value of unknown type")
    encoded = _encode(value.copy(), value.type) if _needs_rewrite(value.type) else value.copy()
    if value.type.this in _TEXT_LEAVES:
        encoded = _call("to_json", encoded)  # a bare leaf's text is a JSON string, not JSON
    if node.args.get("to_json"):
        null: exp.Expr = _unsupported("TO_JSON of NULL")
    else:
        null = exp.Cast(this=exp.Literal.string("null"), to=exp.DataType.build("JSON"))
    value.replace(
        exp.Case(
            ifs=[exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=null)],
            default=encoded,
        )
    )


def _unsupported(construct: str) -> exp.Expr:
    return exp.Anonymous(
        this="error", expressions=[exp.Literal.string(UNSUPPORTED_ERROR_PREFIX + construct)]
    )


def _call(name: str, *args: exp.Expr | str) -> exp.Expr:
    return exp.Anonymous(
        this=name,
        expressions=[exp.Literal.string(a) if isinstance(a, str) else a for a in args],
    )


def _cast(value: exp.Expr, duckdb_type: str) -> exp.Expr:
    return exp.Cast(this=value, to=exp.DataType.build(duckdb_type, dialect="duckdb"))


def _json_text(text: str) -> exp.Expr:
    return _cast(exp.Literal.string(text), "JSON")


def _needs_rewrite(data_type: exp.DataType) -> bool:
    """Whether a leaf that _encode rewrites is inside; raises for a leaf with no encoding."""
    kind = data_type.this
    if kind in (exp.DType.ARRAY, exp.DType.STRUCT):
        inner = [
            f.args["kind"] if isinstance(f, exp.ColumnDef) else f for f in data_type.expressions
        ]
        return any(_needs_rewrite(t) for t in inner if isinstance(t, exp.DataType))
    if kind in _REWRITTEN:
        return True
    if kind not in _SAME_JSON:
        raise CrucibleUnsupportedError(f"TO_JSON_STRING of a {data_type.sql('bigquery')} value")
    return False


def _fraction(six_digits: exp.Expr) -> exp.Expr:
    """'' for whole seconds, '.ddd' for whole milliseconds, else '.dddddd' (recorded)."""
    return exp.Case(
        ifs=[
            exp.If(
                this=exp.EQ(this=six_digits.copy(), expression=exp.Literal.string("000000")),
                true=exp.Literal.string(""),
            ),
            exp.If(
                this=_call("suffix", six_digits.copy(), "000"),
                true=_call("concat", ".", _call("left", six_digits.copy(), exp.Literal.number(3))),
            ),
        ],
        default=_call("concat", ".", six_digits.copy()),
    )


def _datetime_text(naive: exp.Expr, suffix: str) -> exp.Expr:
    return _call(
        "concat",
        _call("strftime", naive.copy(), "%Y-%m-%dT%H:%M:%S"),
        _fraction(_call("strftime", naive.copy(), "%f")),
        suffix,
    )


def _leaf(value: exp.Expr, kind: exp.DType) -> exp.Expr:
    """BigQuery's JSON for one non-NULL leaf: VARCHAR (a JSON string) or JSON (embedded)."""
    if kind == exp.DType.TIMESTAMPTZ:
        return _datetime_text(_cast(_cast(value, "TIMESTAMPTZ"), "TIMESTAMP"), "Z")
    if kind == exp.DType.TIMESTAMP:
        return _datetime_text(_cast(value, "TIMESTAMP"), "")
    if kind == exp.DType.TIME:
        # DuckDB writes a TIME as HH:MM:SS[.f...] with trailing zeros trimmed.
        text = _cast(_cast(value, "TIME"), "VARCHAR")
        six = _call(
            "rpad", _call("substr", text.copy(), exp.Literal.number(10)), exp.Literal.number(6), "0"
        )
        return _call("concat", _call("left", text, exp.Literal.number(8)), _fraction(six))
    if kind in (exp.DType.BLOB, exp.DType.BINARY, exp.DType.VARBINARY):
        return _call("base64", value)
    if kind == exp.DType.DECIMAL:
        trimmed = numeric_text(value.copy())
        whole = exp.Not(this=_call("contains", trimmed.copy(), "."))
        exact = exp.Between(
            this=value.copy(),
            low=exp.Literal.number(-_MAX_EXACT_DOUBLE),
            high=exp.Literal.number(_MAX_EXACT_DOUBLE),
        )
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.And(this=whole.copy(), expression=exact),
                    true=_cast(trimmed.copy(), "JSON"),
                ),
                exp.If(this=whole, true=_unsupported("TO_JSON_STRING of a NUMERIC beyond 2^53")),
            ],
            default=_call("to_json", trimmed),
        )
    if kind == exp.DType.DOUBLE:
        return exp.Case(
            ifs=[
                exp.If(this=_call("isnan", value.copy()), true=_json_text('"NaN"')),
                exp.If(
                    this=exp.EQ(
                        this=value.copy(), expression=_cast(exp.Literal.string("inf"), "DOUBLE")
                    ),
                    true=_json_text('"Infinity"'),
                ),
                exp.If(
                    this=exp.EQ(
                        this=value.copy(), expression=_cast(exp.Literal.string("-inf"), "DOUBLE")
                    ),
                    true=_json_text('"-Infinity"'),
                ),
                # A whole number is written without ".0" (778.0 -> 778: Phase 4 differential run
                # and corpus json/to_json_string_float_whole*); beyond 2^53 it is unrecorded.
                exp.If(
                    this=exp.And(
                        this=exp.EQ(this=_call("trunc", value.copy()), expression=value.copy()),
                        expression=exp.LT(
                            this=_call("abs", value.copy()),
                            expression=exp.Literal.number(_MAX_EXACT_DOUBLE),
                        ),
                    ),
                    true=_cast(_cast(_cast(value.copy(), "BIGINT"), "VARCHAR"), "JSON"),
                ),
                exp.If(
                    this=exp.EQ(this=_call("trunc", value.copy()), expression=value.copy()),
                    true=_unsupported("TO_JSON_STRING of a whole FLOAT64 beyond 2^53"),
                ),
            ],
            default=_call("to_json", value),
        )
    raise AssertionError(kind)


def _encode(value: exp.Expr, data_type: exp.DataType) -> exp.Expr:
    """`value` with every leaf in _REWRITTEN replaced by BigQuery's JSON for it."""
    kind = data_type.this
    if kind in _REWRITTEN:
        return exp.Case(
            ifs=[exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=exp.Null())],
            default=_leaf(value, kind),
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
