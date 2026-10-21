"""queryParameters on jobs.query and jobs.insert (server/bigquery/params.py, parameters.py)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.test_bigquery_server import client, insert_query, make_dataset, run_query

__all__ = ["client"]  # the fixture


def scalar(name: str | None, kind: str, value: Any) -> dict[str, Any]:
    param: dict[str, Any] = {"parameterType": {"type": kind}, "parameterValue": {"value": value}}
    return param | ({"name": name} if name else {})


def cells(body: dict[str, Any]) -> list[Any]:
    return [cell["v"] for row in body["rows"] for cell in row["f"]]


def test_named_scalars_of_each_type(client):
    params = [
        scalar("i", "INT64", "5"),
        scalar("f", "FLOAT64", 1.5),
        scalar("n", "NUMERIC", "1.25"),
        scalar("b", "BOOL", "true"),
        scalar("s", "STRING", "x"),
        scalar("y", "BYTES", "Af8="),
        scalar("d", "DATE", "2024-02-29"),
        scalar("dt", "DATETIME", "2024-01-02T03:04:05.123456"),
        scalar("tm", "TIME", "04:05:06.500000"),
        scalar("ts", "TIMESTAMP", "2024-01-02 03:04:05.123456+00:00"),
        scalar("j", "JSON", '{"a": 1}'),
        scalar("nul", "STRING", None),
    ]
    sql = "SELECT @i + 1, @f, @n, @b, @s, @y, @d, @dt, @tm, @ts, TO_JSON_STRING(@j), @nul"
    body = run_query(client, sql, queryParameters=params, parameterMode="NAMED").json()
    assert cells(body) == [
        "6", "1.5", "1.25", "true", "x", "Af8=", "2024-02-29", "2024-01-02T03:04:05.123456",
        "04:05:06.500000", "1.704164645123456E9", '{"a":1}', None,
    ]  # fmt: skip


def test_positional_parameters_bind_in_order(client):
    params = [scalar(None, "INT64", "2"), scalar(None, "STRING", "b")]
    body = run_query(client, "SELECT ? * 10, ?", queryParameters=params, parameterMode="POSITIONAL")
    assert cells(body.json()) == ["20", "b"]


def test_array_and_struct_parameters(client):
    params = [
        {
            "name": "a",
            "parameterType": {"type": "ARRAY", "arrayType": {"type": "INT64"}},
            "parameterValue": {"arrayValues": [{"value": "1"}, {"value": "2"}]},
        },
        {
            "name": "st",
            "parameterType": {
                "type": "STRUCT",
                "structTypes": [{"name": "x", "type": {"type": "INT64"}}],
            },
            "parameterValue": {"structValues": {"x": {"value": "7"}}},
        },
    ]
    body = run_query(client, "SELECT ARRAY_LENGTH(@a), @st.x", queryParameters=params).json()
    assert cells(body) == ["2", "7"]


def test_a_quote_in_a_string_parameter_cannot_change_the_query(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (k INT64, s STRING)")
    insert_query(client, "INSERT INTO ds.t VALUES (1, 'a'), (2, 'b')")
    attack = "a' OR '1'='1"
    params = [scalar("s", "STRING", attack)]
    body = run_query(client, "SELECT COUNT(*) FROM ds.t WHERE s = @s", queryParameters=params)
    assert cells(body.json()) == ["0"]
    body = run_query(client, "SELECT @s", queryParameters=params)
    assert cells(body.json()) == [attack]


def test_parameters_reach_dml_through_jobs_insert(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (k INT64)")
    extra = {"queryParameters": [scalar("k", "INT64", "3")], "parameterMode": "NAMED"}
    job = insert_query(client, "INSERT INTO ds.t VALUES (@k)", configuration_extra=extra).json()
    assert "errorResult" not in job["status"]
    assert cells(run_query(client, "SELECT k FROM ds.t").json()) == ["3"]


def test_a_missing_parameter_is_an_invalid_query(client):
    response = run_query(client, "SELECT @nope", queryParameters=[scalar("x", "INT64", "1")])
    assert response.status_code == 400
    assert "nope" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "param",
    [
        scalar("x", "INT64", "1.5"),
        scalar("x", "TIMESTAMP", "2024-01-02 03:04:05+01:00"),
        scalar("x", "GEOGRAPHY", "POINT(1 2)"),
        {"name": "x"},
    ],
)
def test_an_unrecorded_value_or_type_is_unsupported(client, param):
    response = run_query(client, "SELECT @x", queryParameters=[param])
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")
