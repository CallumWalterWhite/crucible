"""tabledata.insertAll (streaming inserts) and the streaming-buffer DML refusal."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.unit.test_bigquery_server import API, client, insert_query, make_dataset, run_query

__all__ = ["client"]  # the fixture


def insert_all(client: TestClient, rows: list[dict[str, Any]], table: str = "t", **flags: Any):
    body = {"rows": [{"json": r} for r in rows], **flags}
    return client.post(f"{API}/datasets/ds/tables/{table}/insertAll", json=body)


def query_rows(client: TestClient, sql: str) -> list[list[Any]]:
    body = run_query(client, sql).json()
    return [[cell["v"] for cell in row["f"]] for row in body.get("rows", [])]


@pytest.fixture
def table(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (k INT64, s STRING)")
    return client


def test_every_type_in_the_client_spellings_round_trips(client):
    make_dataset(client)
    insert_query(
        client,
        "CREATE TABLE ds.all_types (i INT64, f FLOAT64, n NUMERIC, b BOOL, s STRING, y BYTES, "
        "d DATE, dt DATETIME, tm TIME, ts TIMESTAMP, j JSON, a ARRAY<INT64>, "
        "r STRUCT<x INT64, z STRING>)",
    )
    row = {
        "i": "5",
        "f": 1.5,
        "n": "1.25",
        "b": "true",
        "s": "x",
        "y": "Af8=",
        "d": "2024-02-29",
        "dt": "2024-01-02T03:04:05.123456",
        "tm": "04:05:06.500000",
        "ts": "2024-01-02T03:04:05.123456Z",
        "j": '{"a": 1}',
        "a": ["1", "2"],
        "r": {"x": "3", "z": "q"},
    }
    response = insert_all(client, [row], table="all_types")
    assert response.status_code == 200
    assert response.json() == {"kind": "bigquery#tableDataInsertAllResponse"}
    [got] = query_rows(
        client,
        "SELECT i, f, n, b, s, y, d, dt, tm, ts, TO_JSON_STRING(j), ARRAY_LENGTH(a), r.x, r.z "
        "FROM ds.all_types",
    )
    assert got == [
        "5", "1.5", "1.25", "true", "x", "Af8=", "2024-02-29", "2024-01-02T03:04:05.123456",
        "04:05:06.500000", "1.704164645123456E9", '{"a":1}', "2", "3", "q",
    ]  # fmt: skip


def test_plain_json_values_from_insert_rows_json(table):
    assert insert_all(table, [{"k": 7, "s": "a"}, {"k": 8}]).json() == {
        "kind": "bigquery#tableDataInsertAllResponse"
    }
    assert query_rows(table, "SELECT k, s FROM ds.t ORDER BY k") == [["7", "a"], ["8", None]]


def test_a_bad_row_stops_the_request_without_skip_invalid_rows(table):
    body = insert_all(table, [{"k": 1}, {"k": "abc"}, {"nope": 1}]).json()
    stopped = {"reason": "stopped", "location": "", "debugInfo": "", "message": ""}
    bad_int = {"reason": "invalid", "location": "k"} | {
        "message": "Cannot convert value to integer (bad value): abc"
    }
    unknown = {"reason": "invalid", "location": "nope", "message": "no such field: nope."}
    assert body["insertErrors"] == [
        {"index": 0, "errors": [stopped]},
        {"index": 1, "errors": [stopped | bad_int]},
        {"index": 2, "errors": [stopped | unknown]},
    ]
    assert query_rows(table, "SELECT COUNT(*) FROM ds.t") == [["0"]]


def test_skip_invalid_rows_inserts_the_valid_ones(table):
    body = insert_all(table, [{"k": 1}, {"k": "abc"}], skipInvalidRows=True).json()
    assert [e["index"] for e in body["insertErrors"]] == [1]
    assert query_rows(table, "SELECT k FROM ds.t") == [["1"]]


def test_ignore_unknown_values_drops_unknown_fields(table):
    response = insert_all(table, [{"k": 1, "nope": 2}], ignoreUnknownValues=True)
    assert "insertErrors" not in response.json()
    assert query_rows(table, "SELECT k FROM ds.t") == [["1"]]


@pytest.mark.parametrize("value", [1.5, True, "1e3", {"x": 1}])
def test_an_unconfirmed_spelling_is_unsupported(table, value):
    response = insert_all(table, [{"k": value}])
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible: insertAll")
    assert query_rows(table, "SELECT COUNT(*) FROM ds.t") == [["0"]]


def test_insert_all_into_a_missing_table_is_a_404(client):
    make_dataset(client)
    response = insert_all(client, [{"k": 1}], table="missing")
    assert response.status_code == 404


def test_a_template_suffix_is_unsupported(table):
    response = insert_all(table, [{"k": 1}], templateSuffix="_x")
    assert response.status_code == 400
    assert "templateSuffix" in response.json()["error"]["message"]


@pytest.mark.parametrize("sql", ["UPDATE ds.t SET k = 2 WHERE TRUE", "DELETE FROM ds.t WHERE TRUE"])
def test_dml_over_streamed_rows_is_refused_until_truncate(table, sql):
    insert_all(table, [{"k": 1}])
    job = insert_query(table, sql).json()
    assert "would affect rows in the streaming buffer" in job["status"]["errorResult"]["message"]
    insert_query(table, "TRUNCATE TABLE ds.t")
    assert "errorResult" not in insert_query(table, sql).json()["status"]


def test_create_or_replace_clears_the_streaming_buffer(table):
    insert_all(table, [{"k": 1}])
    insert_query(table, "CREATE OR REPLACE TABLE ds.t (k INT64, s STRING)")
    job = insert_query(table, "DELETE FROM ds.t WHERE TRUE").json()
    assert "errorResult" not in job["status"]
