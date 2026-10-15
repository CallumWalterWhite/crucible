"""tables.get and tables.list report PARTITION BY / CLUSTER BY as REST Table fields."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from crucible.engine import Engine
from crucible.server.app import create_app

API = "/bigquery/v2/projects/p"


@pytest.fixture
def client():
    engine = Engine()
    with TestClient(create_app("bigquery", engine)) as c:
        c.post(f"{API}/datasets", json={"datasetReference": {"datasetId": "d"}})
        yield c
    engine.close()


def run(client: TestClient, sql: str) -> None:
    response = client.post(f"{API}/queries", json={"query": sql, "useLegacySql": False})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        (
            "CREATE TABLE d.t (d DATE, s STRING, n INT64) PARTITION BY d CLUSTER BY s, n",
            {
                "timePartitioning": {"type": "DAY", "field": "d"},
                "clustering": {"fields": ["s", "n"]},
            },
        ),
        (
            "CREATE TABLE d.t PARTITION BY TIMESTAMP_TRUNC(ts, HOUR) "
            "AS SELECT TIMESTAMP '2024-01-01 00:00:00+00' AS ts",
            {"timePartitioning": {"type": "HOUR", "field": "ts"}},
        ),
        (
            "CREATE TABLE d.t PARTITION BY DATE_TRUNC(d, MONTH) AS SELECT DATE '2024-01-01' AS d",
            {"timePartitioning": {"type": "MONTH", "field": "d"}},
        ),
        (
            "CREATE TABLE d.t PARTITION BY RANGE_BUCKET(n, GENERATE_ARRAY(0, 100, 10)) "
            "AS SELECT 1 AS n",
            {
                "rangePartitioning": {
                    "field": "n",
                    "range": {"start": "0", "end": "100", "interval": "10"},
                }
            },
        ),
        ("CREATE TABLE d.t (n INT64)", {}),
    ],
)
def test_partitioning_and_clustering_are_reported(client, ddl, expected):
    run(client, ddl)
    keys = ("timePartitioning", "rangePartitioning", "clustering")
    table = client.get(f"{API}/datasets/d/tables/t").json()
    assert {k: table[k] for k in keys if k in table} == expected
    [entry] = client.get(f"{API}/datasets/d/tables").json()["tables"]
    assert {k: entry[k] for k in keys if k in entry} == expected
