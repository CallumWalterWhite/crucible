"""NEWLINE_DELIMITED_JSON load parsing (server/bigquery/load.py parse_ndjson).

Values take the insertAll spellings (stream.py) and no others. No NDJSON load is recorded yet, so
a row BigQuery would refuse is unsupported here, not rejected: record-api's ndjson_* cases record
BigQuery's answer, and the tests below pin the refusal until then.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest

from crucible.engine import CrucibleUnsupportedError
from crucible.server.bigquery.load import parse_ndjson

SCHEMA: list[dict[str, Any]] = [
    {"name": "id", "type": "INTEGER"},
    {"name": "s", "type": "STRING"},
]
NESTED: list[dict[str, Any]] = [
    {"name": "a", "type": "INTEGER", "mode": "REPEATED"},
    {
        "name": "r",
        "type": "RECORD",
        "fields": [{"name": "x", "type": "INTEGER"}, {"name": "z", "type": "STRING"}],
    },
]


def parse(
    data: bytes, fields: list[dict[str, Any]] = SCHEMA, *, ignore_unknown: bool = False
) -> Any:
    return parse_ndjson(data, fields, ignore_unknown=ignore_unknown)


def test_rows_load_in_schema_order_whatever_the_key_order_is():
    assert parse(b'{"s": "a", "id": 1}\n{"id": "2", "s": "b"}') == [(1, "a"), (2, "b")]


def test_a_missing_field_is_null():
    assert parse(b'{"id": 1}') == [(1, None)]


def test_a_final_newline_is_optional_and_an_empty_file_has_no_rows():
    assert parse(b'{"id": 1}\n') == parse(b'{"id": 1}') == [(1, None)]
    assert parse(b"") == []


def test_the_client_spellings_of_each_type_load_as_their_values():
    fields = [
        {"name": "k", "type": "INTEGER"},
        {"name": "f", "type": "FLOAT"},
        {"name": "n", "type": "NUMERIC"},
        {"name": "b", "type": "BOOLEAN"},
        {"name": "s", "type": "STRING"},
        {"name": "y", "type": "BYTES"},
        {"name": "d", "type": "DATE"},
        {"name": "dt", "type": "DATETIME"},
        {"name": "tm", "type": "TIME"},
        {"name": "ts", "type": "TIMESTAMP"},
        {"name": "j", "type": "JSON"},
    ]
    row = {
        "k": 1,
        "f": 1.5,
        "n": "1.25",
        "b": True,
        "s": "x",
        "y": "Af8=",
        "d": "2024-02-29",
        "dt": "2024-01-02T03:04:05.123456",
        "tm": "04:05:06.500000",
        "ts": "2024-01-02T03:04:05.123456Z",
        "j": '{"a": 1}',
    }
    [loaded] = parse(json.dumps(row).encode(), fields)
    assert loaded == (
        1,
        1.5,
        Decimal("1.25"),
        True,
        "x",
        b"\x01\xff",
        date(2024, 2, 29),
        datetime(2024, 1, 2, 3, 4, 5, 123456),
        time(4, 5, 6, 500000),
        datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
        '{"a": 1}',
    )


def test_the_standard_type_names_are_the_legacy_ones():
    fields = [{"name": "k", "type": "INT64"}, {"name": "b", "type": "BOOL"}]
    assert parse(b'{"k": 1, "b": true}', fields) == [(1, True)]


def test_nested_record_and_repeated_fields_load():
    assert parse(b'{"a": [1, "2"], "r": {"x": 3, "z": "q"}}', NESTED) == [
        ([1, 2], {"x": 3, "z": "q"})
    ]


def test_a_standard_struct_name_loads_as_a_record():
    fields = [{"name": "r", "type": "STRUCT", "fields": [{"name": "x", "type": "INT64"}]}]
    assert parse(b'{"r": {"x": 3}}', fields) == [({"x": 3},)]


def test_an_unknown_field_is_unsupported_unless_ignored():
    with pytest.raises(CrucibleUnsupportedError, match="NDJSON line 1: no such field: nope"):
        parse(b'{"id": 1, "nope": 2}')
    assert parse(b'{"id": 1, "nope": 2}', ignore_unknown=True) == [(1, None)]


def test_an_unknown_nested_field_is_unsupported_even_when_ignored():
    with pytest.raises(CrucibleUnsupportedError, match="unknown field"):
        parse(b'{"r": {"x": 1, "q": 2}}', NESTED, ignore_unknown=True)


@pytest.mark.parametrize(
    "fields",
    [
        [{"name": "id", "type": "INTEGER", "mode": "REQUIRED"}],
        [
            {
                "name": "r",
                "type": "RECORD",
                "fields": [{"name": "x", "type": "INTEGER", "mode": "REQUIRED"}],
            }
        ],
    ],
    ids=["top level", "inside a record"],
)
def test_a_required_column_is_unsupported_because_a_missing_value_is_a_rejection(fields):
    with pytest.raises(CrucibleUnsupportedError, match="REQUIRED column"):
        parse(b"{}", fields)


@pytest.mark.parametrize(
    "line",
    [
        b'{"id": "abc"}',  # a rejection in BigQuery; its message is not recorded
        b'{"id": 9223372036854775808}',  # out of INT64 range
        b'{"id": 1.5}',  # a JSON float for an INTEGER
        b'{"s": 2}',  # a number for a STRING
    ],
)
def test_a_value_outside_the_confirmed_spellings_is_unsupported(line):
    with pytest.raises(CrucibleUnsupportedError):
        parse(line)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b'{"id": 1}\n\n{"id": 2}', "NDJSON blank line 2"),
        (b'{"id": 1}\r\n{"id": 2}', "NDJSON with a carriage return"),
        (b'\xef\xbb\xbf{"id": 1}', "NDJSON with a byte order mark"),
        (b'{"id": 1', "NDJSON line 1 is not JSON"),
        (b'{"id": NaN}', "NDJSON with the non-JSON constant NaN"),
        (b'{"s": "\xff"}', "NDJSON that is not UTF-8"),
        (b"[1, 2]", "not an object"),
    ],
)
def test_a_file_outside_the_confirmed_shape_is_unsupported(data, message):
    with pytest.raises(CrucibleUnsupportedError, match=message):
        parse(data)
