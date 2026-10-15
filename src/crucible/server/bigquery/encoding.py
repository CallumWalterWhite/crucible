"""BigQuery REST wire encoding: schemas and `f`/`v` row values.

Every rule here is taken from recorded responses (`tests/fixtures/bigquery_api/`), cited by fixture:
- schema: legacy type names (INTEGER, FLOAT, BOOLEAN, RECORD), `mode` NULLABLE/REPEATED, nested
  `fields` (get_query_results.json);
- values are strings; NULL is null; an ARRAY is a list of {"v": ...}; a STRUCT is {"f": [...]};
  a NULL array is [] (get_query_results, row 2);
- FLOAT64 and TIMESTAMP (float seconds) use Java's Double.toString: "1.5", "NaN",
  "1.704164645123456E9", "0.0" (get_query_results); with formatOptions.useInt64Timestamp,
  TIMESTAMP is integer microseconds "1704164645123456" (get_query_results_int64_timestamp);
- DATETIME "2024-01-02T03:04:05.123456", TIME "04:05:06.500000" / "00:00:00", DATE "0001-01-01",
  NUMERIC "-0.000000001", BYTES base64, JSON compact text (get_query_results).
"""

from __future__ import annotations

import base64
import json
import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlglot import exp

from crucible.dialects.bigquery import types
from crucible.engine import CrucibleUnsupportedError

T = exp.DType

_LEGACY_NAMES = {
    "INT64": "INTEGER",
    "FLOAT64": "FLOAT",
    "BOOL": "BOOLEAN",
    "STRING": "STRING",
    "BYTES": "BYTES",
    "NUMERIC": "NUMERIC",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMP",
    "JSON": "JSON",
    "INTERVAL": "INTERVAL",
}


# --- schema ------------------------------------------------------------------------------------
def schema_field(name: str, data_type: exp.DataType, *, with_mode: bool = True) -> dict[str, Any]:
    """One REST schema field. `with_mode=False` omits NULLABLE (tables.get and job statistics
    leave it out: jobs_get_ctas.json; query results include it: 05)."""
    mode = "NULLABLE"
    if data_type.this == T.ARRAY:
        mode = "REPEATED"
        [data_type] = data_type.expressions  # pyright: ignore[reportAssignmentType]
    field: dict[str, Any] = {"name": name}
    if data_type.this == T.STRUCT:
        field["type"] = "RECORD"
    else:
        field["type"] = _LEGACY_NAMES[types.type_name(data_type)]
    if with_mode or mode == "REPEATED":
        field["mode"] = mode
    if data_type.this == T.STRUCT:
        field["fields"] = [
            schema_field(c.name, c.args["kind"], with_mode=with_mode)
            for c in data_type.expressions
            if isinstance(c, exp.ColumnDef)
        ]
    return field


def schema(columns: list[tuple[str, exp.DataType]], *, with_mode: bool = True) -> dict[str, Any]:
    return {"fields": [schema_field(name, t, with_mode=with_mode) for name, t in columns]}


def warehouse_type_from_field(field: dict[str, Any]) -> exp.DataType:
    """The BigQuery type of a REST schema field (load job and tables.patch schemas)."""
    legacy = str(field.get("type", "")).upper()
    by_legacy = {v: k for k, v in _LEGACY_NAMES.items()} | {
        "INT64": "INT64",
        "FLOAT64": "FLOAT64",
        "BOOL": "BOOL",
        "BIGNUMERIC": "BIGNUMERIC",
        "GEOGRAPHY": "GEOGRAPHY",
    }
    if legacy in ("RECORD", "STRUCT"):
        inner = ", ".join(
            f"`{sub['name']}` {types.type_name(warehouse_type_from_field(sub))}"
            for sub in field.get("fields", [])
        )
        base = f"STRUCT<{inner}>"
    elif legacy in by_legacy:
        base = by_legacy[legacy]
    else:
        raise CrucibleUnsupportedError(f"schema field type {legacy}")
    if str(field.get("mode", "NULLABLE")).upper() == "REPEATED":
        base = f"ARRAY<{base}>"
    return types.parse(base)


# --- values ------------------------------------------------------------------------------------
def java_double(value: float) -> str:
    """Java's Double.toString, which BigQuery uses for FLOAT64 and float-seconds TIMESTAMP.

    Shortest round-trip digits (as Python's repr); plain notation for 1e-3 <= |x| < 1e7 with at
    least one fractional digit ("1.5", "100.0"), else "d.dddE<exp>" ("1.704164645123456E9").
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    digits = Decimal(repr(magnitude))
    if 1e-3 <= magnitude < 1e7:
        text = format(digits, "f")
        return sign + (text if "." in text else text + ".0")
    _, digit_tuple, exponent = digits.normalize().as_tuple()
    numerals = "".join(str(d) for d in digit_tuple)
    assert isinstance(exponent, int)
    power = exponent + len(numerals) - 1
    mantissa = numerals[0] + "." + (numerals[1:] or "0")
    return f"{sign}{mantissa}E{power}"


def _timestamp_micros(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _tabledata_seconds(micros: int) -> str:
    """tabledata.list writes float-seconds TIMESTAMPs unlike getQueryResults: "0.0" and
    "1704164645.123456" are recorded (tabledata_list.json). Other shapes (whole seconds, a
    fraction with trailing zeros, before 1970) are unrecorded and raise. The clients ask for
    int64 timestamps instead
    (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4264)."""
    seconds, fraction = divmod(micros, 1_000_000)
    if micros == 0:
        return "0.0"
    if micros > 0 and fraction % 10 != 0:
        return f"{seconds}.{fraction:06d}"
    raise CrucibleUnsupportedError("tabledata.list float TIMESTAMP of this shape (use int64)")


def encode_value(
    value: Any, data_type: exp.DataType, *, int64_timestamp: bool = False, tabledata: bool = False
) -> Any:
    """One cell's `v`: a string, null, a list of {"v": ...}, or {"f": [...]}."""
    kind = data_type.this
    if kind == T.ARRAY:
        [element] = data_type.expressions
        assert isinstance(element, exp.DataType)
        if value is None:
            return []  # a NULL array is written as [] (get_query_results.json, row 2)
        return [
            {"v": encode_value(v, element, int64_timestamp=int64_timestamp, tabledata=tabledata)}
            for v in value
        ]
    if value is None:
        return None
    if kind == T.STRUCT:
        fields = [c for c in data_type.expressions if isinstance(c, exp.ColumnDef)]
        return {
            "f": [
                {
                    "v": encode_value(
                        value.get(c.name),
                        c.args["kind"],
                        int64_timestamp=int64_timestamp,
                        tabledata=tabledata,
                    )
                }
                for c in fields
            ]
        }
    name = types.type_name(data_type)
    if name == "INT64":
        return str(int(value))
    if name == "FLOAT64":
        return java_double(float(value))
    if name == "NUMERIC":
        number = Decimal(value)
        return "0" if number == 0 else format(number.normalize(), "f")
    if name == "BOOL":
        return "true" if value else "false"
    if name == "STRING":
        return str(value)
    if name == "BYTES":
        return base64.b64encode(bytes(value)).decode("ascii")
    if name == "DATE" and isinstance(value, date):
        return value.isoformat()
    if name == "DATETIME" and isinstance(value, datetime):
        return value.isoformat()
    if name == "TIME" and isinstance(value, time):
        return value.isoformat()
    if name == "TIMESTAMP" and isinstance(value, datetime):
        micros = _timestamp_micros(value)
        if int64_timestamp:
            return str(micros)
        return _tabledata_seconds(micros) if tabledata else java_double(micros / 1_000_000)
    if name == "JSON":
        # Compact text; a JSON null is the text "null", not a SQL NULL (get_query_results, row 3).
        parsed = json.loads(value) if isinstance(value, str) else value
        return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    # INTERVAL: DuckDB hands Python a timedelta that has folded months into 30-day days.
    raise CrucibleUnsupportedError(f"{name} value in a query result")


def encode_rows(
    rows: list[tuple[Any, ...]],
    column_types: list[exp.DataType],
    *,
    int64_timestamp: bool = False,
    tabledata: bool = False,
) -> list[dict[str, Any]]:
    return [
        {
            "f": [
                {"v": encode_value(v, t, int64_timestamp=int64_timestamp, tabledata=tabledata)}
                for v, t in zip(row, column_types, strict=True)
            ]
        }
        for row in rows
    ]
