"""BigQuery type mapping and canonical values (ADR 0014).

SQLGlot represents BigQuery and DuckDB types with the same `DType` enum (BigQuery DATETIME is
TIMESTAMP, BigQuery TIMESTAMP is TIMESTAMPTZ, ...), so one set of rules covers both the types
`annotate_types` gives the BigQuery AST and the types DuckDB reports for results.
"""

from __future__ import annotations

import base64
import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlglot import exp

from crucible.engine import CrucibleUnsupportedError

T = exp.DType

# BigQuery type -> DuckDB storage type, for scalar types whose storage needs no parameters.
_SCALAR_STORAGE: dict[exp.DType, str] = {
    T.BIGINT: "BIGINT",
    T.DOUBLE: "DOUBLE",
    T.BOOLEAN: "BOOLEAN",
    T.TEXT: "VARCHAR",
    T.VARCHAR: "VARCHAR",
    T.BINARY: "BLOB",
    T.VARBINARY: "BLOB",
    T.BLOB: "BLOB",
    T.DATE: "DATE",
    T.TIMESTAMP: "TIMESTAMP",  # BigQuery DATETIME
    T.TIME: "TIME",
    T.TIMESTAMPTZ: "TIMESTAMPTZ",  # BigQuery TIMESTAMP
    T.JSON: "JSON",
    T.INTERVAL: "INTERVAL",
    T.NULL: "BIGINT",  # an untyped NULL is INT64 in BigQuery
}

# Type enum -> BigQuery name, for the same scalar types.
_SCALAR_NAMES: dict[exp.DType, str] = {
    T.BIGINT: "INT64",
    T.DOUBLE: "FLOAT64",
    T.BOOLEAN: "BOOL",
    T.TEXT: "STRING",
    T.VARCHAR: "STRING",
    T.BINARY: "BYTES",
    T.VARBINARY: "BYTES",
    T.BLOB: "BYTES",
    T.DATE: "DATE",
    T.TIMESTAMP: "DATETIME",
    T.TIME: "TIME",
    T.TIMESTAMPTZ: "TIMESTAMP",
    T.JSON: "JSON",
    T.INTERVAL: "INTERVAL",
    T.NULL: "INT64",
}

NUMERIC_PRECISION, NUMERIC_SCALE = 38, 9
_UNSUPPORTED = {
    T.BIGDECIMAL: "BIGNUMERIC (precision 76 exceeds DuckDB's 38)",
    T.GEOGRAPHY: "GEOGRAPHY",
}


def parse(type_sql: str) -> exp.DataType:
    """A BigQuery type string, e.g. 'ARRAY<STRUCT<a INT64>>'."""
    return exp.DataType.build(type_sql, dialect="bigquery")


def _decimal_params(data_type: exp.DataType) -> tuple[int, int] | None:
    params = [p for p in data_type.expressions if isinstance(p, exp.DataTypeParam)]
    if not params:
        return None
    values = [int(p.this.name) for p in params]
    return values[0], values[1] if len(values) > 1 else 0


def _fields(data_type: exp.DataType) -> list[tuple[str, exp.DataType]]:
    fields: list[tuple[str, exp.DataType]] = []
    for field in data_type.expressions:
        if isinstance(field, exp.ColumnDef):
            kind = field.args.get("kind")
            assert isinstance(kind, exp.DataType)
            fields.append((field.name, kind))
        else:  # an anonymous struct field: STRUCT<INT64>
            assert isinstance(field, exp.DataType)
            fields.append(("", field))
    return fields


def _element(data_type: exp.DataType) -> exp.DataType:
    [inner] = data_type.expressions
    assert isinstance(inner, exp.DataType)
    return inner


# SQLGlot's annotator sometimes types BigQuery values with narrower enums (an integer literal as
# INT, ...). BigQuery has one integer and one float type, so these all mean INT64 / FLOAT64.
_ANNOTATION_ALIASES = {
    T.TINYINT: T.BIGINT,
    T.SMALLINT: T.BIGINT,
    T.INT: T.BIGINT,
    T.FLOAT: T.DOUBLE,
    T.DATETIME: T.TIMESTAMP,
}
# Spellings of BigQuery DATETIME / DuckDB TIMESTAMP (without time zone) across SQLGlot dialects.
_SAME_TYPE = {T.DATETIME: T.TIMESTAMP, T.TIMESTAMPNTZ: T.TIMESTAMP}


def normalize(data_type: exp.DataType) -> exp.DataType:
    """An annotated type in canonical enums: INT/SMALLINT/TINYINT -> BIGINT (INT64), FLOAT ->
    DOUBLE, DATETIME -> TIMESTAMP, recursively through ARRAY and STRUCT."""
    out = data_type.copy()
    for node in out.find_all(exp.DataType):
        kind = node.this
        node.set("this", _ANNOTATION_ALIASES.get(kind, _SAME_TYPE.get(kind, kind)))
    return out


def _has_unknown(data_type: exp.DataType) -> bool:
    return any(t.this == T.UNKNOWN for t in data_type.find_all(exp.DataType))


def supertype(left: exp.DataType, right: exp.DataType) -> exp.DataType | None:
    """BigQuery's common supertype of two types (set operations, COALESCE), or None."""
    # `[]` (ARRAY<UNKNOWN>) next to a typed array: the typed side is the supertype.
    if _has_unknown(left) != _has_unknown(right):
        known, unknown = (right, left) if _has_unknown(left) else (left, right)
        return known if coerces(unknown, known) else None
    if left.this == T.NULL or coerces(right, left):
        return right if left.this == T.NULL else left
    if coerces(left, right):
        return right
    return None


def storage_type(data_type: exp.DataType) -> exp.DataType:
    """The DuckDB type that stores a BigQuery type. Raises for types Crucible cannot hold."""
    kind = _ANNOTATION_ALIASES.get(data_type.this, _SAME_TYPE.get(data_type.this, data_type.this))
    if kind == T.UNKNOWN:
        raise CrucibleUnsupportedError("a value whose BigQuery type Crucible cannot determine")
    if kind in _UNSUPPORTED:
        raise CrucibleUnsupportedError(_UNSUPPORTED[kind])
    if kind == T.DECIMAL:
        precision, scale = _decimal_params(data_type) or (NUMERIC_PRECISION, NUMERIC_SCALE)
        return exp.DataType.build(f"DECIMAL({precision}, {scale})", dialect="duckdb")
    if kind == T.ARRAY:
        return exp.DataType(
            this=T.ARRAY, expressions=[storage_type(_element(data_type))], nested=True
        )
    if kind == T.STRUCT:
        columns = [
            exp.ColumnDef(this=exp.to_identifier(name, quoted=True), kind=storage_type(inner))
            for name, inner in _fields(data_type)
        ]
        return exp.DataType(this=T.STRUCT, expressions=columns, nested=True)
    if kind in _SCALAR_STORAGE:
        return exp.DataType.build(_SCALAR_STORAGE[kind], dialect="duckdb")
    raise CrucibleUnsupportedError(f"BigQuery type {data_type.sql('bigquery')}")


def type_name(data_type: exp.DataType) -> str:
    """The BigQuery name of an annotated or DuckDB result type, as the corpus writes it (ADR 0013).

    DuckDB types with no BigQuery equivalent (INTEGER, HUGEINT, DECIMAL(18,3), ...) raise, so a
    result is never reported under a guessed type (ADR 0014, "Fallback").
    """
    kind = _SAME_TYPE.get(data_type.this, data_type.this)
    if kind == T.DECIMAL:
        params = _decimal_params(data_type)
        if params in (None, (NUMERIC_PRECISION, NUMERIC_SCALE)):
            return "NUMERIC"
        raise CrucibleUnsupportedError(f"result type {data_type.sql('duckdb')} (not NUMERIC)")
    if kind == T.ARRAY:
        return f"ARRAY<{type_name(_element(data_type))}>"
    if kind == T.STRUCT:
        fields = ", ".join(
            f"{name} {type_name(inner)}" if name else type_name(inner)
            for name, inner in _fields(data_type)
        )
        return f"STRUCT<{fields}>"
    if kind in _SCALAR_NAMES:
        return _SCALAR_NAMES[kind]
    if kind in _UNSUPPORTED:
        raise CrucibleUnsupportedError(_UNSUPPORTED[kind])
    raise CrucibleUnsupportedError(f"result type {data_type.sql('duckdb')} has no BigQuery type")


# BigQuery implicit coercion (Conversion rules, "Coercion"):
# https://cloud.google.com/bigquery/docs/reference/standard-sql/conversion_rules
_COERCIONS = {
    "INT64": {"NUMERIC", "BIGNUMERIC", "FLOAT64"},
    "NUMERIC": {"BIGNUMERIC", "FLOAT64"},
    "BIGNUMERIC": {"FLOAT64"},
    "DATE": {"DATETIME"},
}
_STRING_LITERAL_TARGETS = {"DATE", "DATETIME", "TIME", "TIMESTAMP"}


def coerces(source: exp.DataType, target: exp.DataType, *, literal_string: bool = False) -> bool:
    """Whether BigQuery implicitly coerces a value of `source` type to `target` (e.g. on INSERT)."""
    if source.this in (T.NULL, T.UNKNOWN):
        return source.this == T.NULL
    if source.this == T.ARRAY and target.this == T.ARRAY:
        # `[]` has no element type of its own and coerces to any array type. Otherwise the element
        # types must match: element-wise array coercion is not documented (corpus case pending).
        element, target_element = _element(source), _element(target)
        if T.UNKNOWN in (element.this, target_element.this) or element.this == T.NULL:
            return True
        return _bq_name(element) == _bq_name(target_element)
    if source.this == T.STRUCT and target.this == T.STRUCT:
        pairs = list(zip(_fields(source), _fields(target), strict=False))
        return len(_fields(source)) == len(_fields(target)) and all(
            coerces(s, t) for (_, s), (_, t) in pairs
        )
    src, dst = _bq_name(source), _bq_name(target)
    if src == dst:
        return True
    if literal_string and src == "STRING":
        return dst in _STRING_LITERAL_TARGETS
    return dst in _COERCIONS.get(src, set())


def _bq_name(data_type: exp.DataType) -> str:
    if data_type.this == T.DECIMAL:
        return "NUMERIC"
    if data_type.this == T.BIGDECIMAL:
        return "BIGNUMERIC"
    return type_name(data_type)


def canonical_value(value: Any, warehouse_type: str) -> Any:
    """JSON-safe rendering for the corpus (ADR 0014, "Canonical values")."""
    return _canonical(value, parse(warehouse_type))


def _canonical(value: Any, data_type: exp.DataType) -> Any:
    if value is None:
        return None
    kind = _SAME_TYPE.get(data_type.this, data_type.this)
    if kind == T.ARRAY:
        inner = _element(data_type)
        return [_canonical(v, inner) for v in value]
    if kind == T.STRUCT:
        return {name: _canonical(value.get(name), inner) for name, inner in _fields(data_type)}
    if kind == T.BIGINT:
        return int(value)
    if kind == T.DOUBLE:
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return number
    if kind == T.DECIMAL:
        number = Decimal(value)
        return "0" if number == 0 else format(number.normalize(), "f")
    if kind == T.BOOLEAN:
        return bool(value)
    if kind in (T.TEXT, T.VARCHAR):
        return str(value)
    if kind in (T.BINARY, T.VARBINARY, T.BLOB):
        return base64.b64encode(bytes(value)).decode("ascii")
    if kind == T.DATE and isinstance(value, date):
        return value.isoformat()
    if kind == T.TIMESTAMP and isinstance(value, datetime):
        return value.isoformat()
    if kind == T.TIME and isinstance(value, time):
        return value.isoformat()
    if kind == T.TIMESTAMPTZ and isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
    if kind == T.JSON:
        return value  # the engine already decoded JSON text into Python values
    raise CrucibleUnsupportedError(f"canonical value for {type_name(data_type)}: {value!r}")
