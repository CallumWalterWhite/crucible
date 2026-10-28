"""Parquet load parsing (parquet.py): the schema wins, types map exactly, the rest is refused."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from crucible.engine import CrucibleUnsupportedError
from crucible.server.bigquery.parquet import parse_parquet

FIELDS: list[dict[str, Any]] = [
    {"name": "k", "type": "INTEGER", "mode": "REQUIRED"},
    {"name": "f", "type": "FLOAT"},
    {"name": "b", "type": "BOOLEAN"},
    {"name": "d", "type": "DATE"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "s", "type": "STRING"},
]


def parquet(**columns: pa.Array) -> bytes:
    sink = io.BytesIO()
    pq.write_table(pa.table(columns), sink)
    return sink.getvalue()


def typed_file(**override: pa.Array) -> bytes:
    columns = {
        "k": pa.array([1, 2], pa.int64()),
        "f": pa.array([1.5, None], pa.float64()),
        "b": pa.array([True, None]),
        "d": pa.array([date(2024, 2, 29), None]),
        "ts": pa.array(
            [datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=UTC), None], pa.timestamp("us", "UTC")
        ),
        "s": pa.array(["x", None]),
    }
    return parquet(**(columns | override))


def test_each_type_loads_in_schema_order():
    data = parquet(s=pa.array(["x"]), k=pa.array([1], pa.int64()))  # file order differs
    table = parse_parquet(data, [{"name": "k", "type": "INT64"}, {"name": "s", "type": "STRING"}])
    assert table.column_names == ["k", "s"]
    rows = parse_parquet(typed_file(), FIELDS).to_pylist()
    assert rows[0] == {
        "k": 1,
        "f": 1.5,
        "b": True,
        "d": date(2024, 2, 29),
        "ts": datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=UTC),
        "s": "x",
    }
    assert rows[1] == {"k": 2, "f": None, "b": None, "d": None, "ts": None, "s": None}


def test_column_names_match_the_schema_without_case():
    table = parse_parquet(parquet(K=pa.array([1], pa.int64())), [{"name": "k", "type": "INTEGER"}])
    assert table.to_pylist() == [{"k": 1}]


def test_an_all_null_column_and_whole_nanosecond_timestamps_load():
    ns = pa.array([1_000, None], pa.timestamp("ns", "UTC"))  # 1 microsecond, exactly
    data = parquet(ts=ns, s=pa.array([None, None], pa.null()))
    table = parse_parquet(
        data, [{"name": "ts", "type": "TIMESTAMP"}, {"name": "s", "type": "STRING"}]
    )
    assert table.column("ts").to_pylist()[0] == datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    assert table.column("s").null_count == 2


@pytest.mark.parametrize(
    ("data", "fields", "construct"),
    [
        (typed_file(k=pa.array([1, None], pa.int64())), FIELDS, "NULL in the REQUIRED field k"),
        (typed_file(k=pa.array([1, 2], pa.int32())), FIELDS, "Parquet column k of type int32"),
        (typed_file(ts=pa.array([0, 1], pa.timestamp("us"))), FIELDS, "Parquet column ts of type"),
        (
            typed_file(ts=pa.array([1, 2], pa.timestamp("ns", "UTC"))),
            FIELDS,
            "cannot hold exactly",
        ),
        (typed_file(extra=pa.array([1, 2])), FIELDS, "Parquet column extra that is not in"),
        (parquet(k=pa.array([1], pa.int64())), FIELDS, "without the schema's column f"),
        (b"not a parquet file", FIELDS, "a Parquet file pyarrow cannot read"),
        (typed_file(), [*FIELDS, {"name": "n", "type": "NUMERIC"}], "into a NUMERIC field"),
        (typed_file(), [{"name": "k", "type": "INTEGER", "mode": "REPEATED"}], "ARRAY<INT64>"),
    ],
)
def test_what_is_not_recorded_is_refused(data, fields, construct):
    with pytest.raises(CrucibleUnsupportedError, match=construct):
        parse_parquet(data, fields)
