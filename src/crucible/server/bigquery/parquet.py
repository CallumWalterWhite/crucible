"""Parquet load jobs (ADR 0015).

pyarrow parses the upload in-process, each column is checked against the load schema, and DuckDB
reads the result as a registered Arrow table, so no file path reaches SQL (ADR 0006). The load
schema wins: columns are matched to it by name, and a REQUIRED field stays REQUIRED although
Arrow marks every column nullable.

Accepted, column type to schema type, without loss: int64 to INT64, double to FLOAT64, bool to
BOOL, date32 to DATE, a UTC timestamp (s, ms, us, or ns with no sub-microsecond part) to
TIMESTAMP, string to STRING, and an all-NULL column to any of them. Everything else is refused
until recorded: other types and pairings, nested and repeated fields, a column missing from the
file or the schema, NULL in a REQUIRED field, and a file pyarrow cannot read.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from crucible.dialects.bigquery import types
from crucible.engine import CrucibleUnsupportedError
from crucible.server.bigquery import encoding

MAX_BYTES = 512 * 1024 * 1024  # read whole into memory, so bounded before parsing (ADR 0015)

_TARGETS: dict[str, tuple[pa.DataType, Callable[[pa.DataType], bool]]] = {
    "INT64": (pa.int64(), pa.types.is_int64),
    "FLOAT64": (pa.float64(), pa.types.is_float64),
    "BOOL": (pa.bool_(), pa.types.is_boolean),
    "DATE": (pa.date32(), pa.types.is_date32),
    "TIMESTAMP": (
        pa.timestamp("us", tz="UTC"),
        lambda t: pa.types.is_timestamp(t) and t.tz in ("UTC", "+00:00", "Etc/UTC"),
    ),
    "STRING": (pa.string(), lambda t: pa.types.is_string(t) or pa.types.is_large_string(t)),
}


def parse_parquet(data: bytes, fields: list[dict[str, Any]]) -> pa.Table:
    """The file's columns in schema order, as the Arrow types DuckDB stores for the schema."""
    if len(data) > MAX_BYTES:
        raise CrucibleUnsupportedError(f"a Parquet load larger than {MAX_BYTES} bytes")
    kinds: list[str] = []
    for field in fields:
        mode = str(field.get("mode", "NULLABLE")).upper()
        kind = types.type_name(encoding.warehouse_type_from_field(field))
        if mode == "REPEATED" or kind not in _TARGETS:
            shown = f"ARRAY<{kind}>" if mode == "REPEATED" else kind.split("<", 1)[0]
            raise CrucibleUnsupportedError(f"Parquet load into a {shown} field")
        kinds.append(kind)
    try:
        # The stubs leave read_table's keyword arguments partly unknown.
        table: pa.Table = pq.read_table(pa.BufferReader(data))  # pyright: ignore[reportUnknownMemberType]
    except (pa.ArrowException, OSError) as exc:
        raise CrucibleUnsupportedError("a Parquet file pyarrow cannot read") from exc
    in_file = {name.lower(): name for name in table.column_names}
    if len(in_file) != len(table.column_names):
        raise CrucibleUnsupportedError("a Parquet file with column names that differ only by case")
    wanted = {str(f["name"]).lower() for f in fields}
    for name in table.column_names:
        if name.lower() not in wanted:
            raise CrucibleUnsupportedError(f"Parquet column {name} that is not in the load schema")
    columns: list[Any] = []  # pyarrow arrays; the stubs type them only partly
    for field, kind in zip(fields, kinds, strict=True):
        name = str(field["name"])
        if name.lower() not in in_file:
            raise CrucibleUnsupportedError(f"a Parquet file without the schema's column {name}")
        column: Any = table.column(in_file[name.lower()])
        target, accepts = _TARGETS[kind]
        if pa.types.is_null(column.type):
            column = pa.array([None] * len(column), type=target)
        elif not accepts(column.type):
            raise CrucibleUnsupportedError(
                f"Parquet column {name} of type {column.type} into a {kind} field"
            )
        else:
            try:
                column = column.cast(target, safe=True)  # raises rather than truncate
            except pa.ArrowInvalid as exc:
                raise CrucibleUnsupportedError(
                    f"Parquet column {name}: a {column.type} value {kind} cannot hold exactly"
                ) from exc
        if str(field.get("mode", "NULLABLE")).upper() == "REQUIRED" and column.null_count:
            raise CrucibleUnsupportedError(f"NULL in the REQUIRED field {name} of a Parquet load")
        columns.append(column)
    return pa.Table.from_arrays(columns, names=[str(f["name"]) for f in fields])
