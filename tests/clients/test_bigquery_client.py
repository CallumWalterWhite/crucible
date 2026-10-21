"""google-cloud-bigquery's public API against a running Crucible (CRUCIBLE_URL).

Marked `clients`; CI runs `pytest -m clients tests/clients -k bigquery` against the oldest and
latest client. Credentials are anonymous and the endpoint is always CRUCIBLE_URL, so nothing here
reaches the real BigQuery. Each test makes its own dataset and deletes it. The server is expected
to implement the BigQuery API subset (docs/architecture.md, "BigQuery API subset"), tabledata.list
included.

Client behaviour the tests depend on, read from google-cloud-bigquery 3.27.0 and 3.46.1:
- `Client.query(..., job_id=...)` with a caller-supplied id re-raises Conflict on a second insert;
  only a generated id attaches to the existing job (`_job_helpers.py`: the `job_id_given` branch
  in `query_jobs_insert`). 3.46 also warns (FutureWarning, `_job_helpers.py`: `validate_job_retry`)
  unless `job_retry=None` is passed with a custom job_id, so the test passes it.
- JSON cells are parsed with `json.loads` (`_helpers.py`: `_json_from_json`).
- `list_rows` reads `tables/{id}/data` (tabledata.list) with `maxResults`, `pageToken`,
  `startIndex` and `formatOptions.useInt64Timestamp=True` (`client.py`: `list_rows`; 3.46.1 at
  :4256-4264, 3.27.0 at :4038-4042).
- `num_dml_affected_rows` is the query job's `statistics.query.numDmlAffectedRows`.
- `query_and_wait` exists in 3.27.0 (`client.py`: `query_and_wait`; it posts to jobs/query
  via `_job_helpers.py`), the floor of the `record-bigquery` extra in pyproject.toml, so no
  version guard is needed.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import decimal
import io
import math
import os
import uuid

import pytest
from google.api_core import exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

CRUCIBLE_URL = os.environ.get("CRUCIBLE_URL")
if not CRUCIBLE_URL:
    pytest.skip("CRUCIBLE_URL is not set", allow_module_level=True)

pytestmark = pytest.mark.clients
PROJECT = "crucible-test"

# Every ADR 0014 type, plus NULL. Literals are spelled the way BigQuery writes them.
TYPES_SQL = """
SELECT
  1 AS i,
  1.5 AS f,
  CAST('NaN' AS FLOAT64) AS nan,
  CAST('inf' AS FLOAT64) AS inf,
  NUMERIC '1.5' AS n,
  TRUE AS b,
  'text' AS s,
  b'ab' AS byt,
  DATE '2024-02-29' AS d,
  DATETIME '2024-02-29 01:02:03.5' AS dt,
  TIME '12:34:56.123456' AS t,
  TIMESTAMP '2024-02-29 01:02:03.123456+00' AS ts,
  JSON '{"k": 1}' AS j,
  [1, 2] AS arr,
  STRUCT(1 AS a, 'y' AS b) AS st,
  [STRUCT(1 AS a), STRUCT(2 AS a)] AS arr_st,
  CAST(NULL AS INT64) AS nul
"""
UTC = datetime.UTC
EXPECTED_TYPES = {
    "i": 1,
    "f": 1.5,
    "n": decimal.Decimal("1.5"),
    "b": True,
    "s": "text",
    "byt": b"ab",
    "d": datetime.date(2024, 2, 29),
    "dt": datetime.datetime(2024, 2, 29, 1, 2, 3, 500000),
    "t": datetime.time(12, 34, 56, 123456),
    "ts": datetime.datetime(2024, 2, 29, 1, 2, 3, 123456, tzinfo=UTC),
    "j": {"k": 1},
    "arr": [1, 2],
    "st": {"a": 1, "b": "y"},
    "arr_st": [{"a": 1}, {"a": 2}],
    "nul": None,
}


def make_client() -> bigquery.Client:
    return bigquery.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": CRUCIBLE_URL},
    )


@pytest.fixture
def client() -> bigquery.Client:
    return make_client()


@pytest.fixture
def dataset(client: bigquery.Client):
    name = f"crucible_{uuid.uuid4().hex[:12]}"
    client.create_dataset(bigquery.Dataset(f"{PROJECT}.{name}"))
    try:
        yield name
    finally:
        client.delete_dataset(f"{PROJECT}.{name}", delete_contents=True, not_found_ok=True)


def assert_types(row) -> None:
    for name, expected in EXPECTED_TYPES.items():
        assert row[name] == expected, name
    assert math.isnan(row["nan"])
    assert row["inf"] == math.inf


def test_bigquery_create_insert_and_select(client, dataset):
    table = f"`{PROJECT}.{dataset}.people`"
    client.query(f"CREATE TABLE {table} (id INT64, name STRING)").result()
    insert = client.query(f"INSERT INTO {table} (id, name) VALUES (1, 'ada'), (2, 'grace')")
    insert.result()
    assert insert.num_dml_affected_rows == 2
    rows = list(client.query(f"SELECT id, name FROM {table} ORDER BY id").result())
    assert [(r.id, r.name) for r in rows] == [(1, "ada"), (2, "grace")]


def test_bigquery_create_table_get_list_delete(client, dataset):
    ref = f"{PROJECT}.{dataset}.inventory"
    schema = [bigquery.SchemaField("sku", "STRING"), bigquery.SchemaField("qty", "INTEGER")]
    client.create_table(bigquery.Table(ref, schema=schema))
    got = client.get_table(ref)
    assert [(f.name, f.field_type) for f in got.schema] == [("sku", "STRING"), ("qty", "INTEGER")]
    client.query(f"INSERT INTO `{ref}` (sku, qty) VALUES ('a', 1), ('b', 2)").result()
    assert client.get_table(ref).num_rows == 2
    assert "inventory" in [t.table_id for t in client.list_tables(f"{PROJECT}.{dataset}")]
    client.delete_table(ref)
    assert "inventory" not in [t.table_id for t in client.list_tables(f"{PROJECT}.{dataset}")]


def test_bigquery_paging_returns_all_rows_in_order(client):
    job = client.query("SELECT x FROM UNNEST(GENERATE_ARRAY(1, 25)) AS x ORDER BY x")
    pages = list(job.result(page_size=10).pages)
    assert len(pages) == 3
    assert [row.x for page in pages for row in page] == list(range(1, 26))


def test_bigquery_every_type_round_trips_through_result(client):
    row = next(iter(client.query(TYPES_SQL).result()))
    assert_types(row)


def test_bigquery_every_type_round_trips_through_query_and_wait(client):
    row = next(iter(client.query_and_wait(TYPES_SQL)))
    assert_types(row)


def test_bigquery_interval_is_not_supported(client):
    with pytest.raises(exceptions.BadRequest, match="Not supported by Crucible"):
        client.query("SELECT INTERVAL 1 DAY AS i").result()


def test_bigquery_interval_not_supported_via_query_and_wait(client):
    with pytest.raises(exceptions.BadRequest, match="Not supported by Crucible"):
        client.query_and_wait("SELECT INTERVAL 1 DAY AS i")


def test_bigquery_job_id_conflict_then_attach(client):
    job_id = f"crucible_fixed_{uuid.uuid4().hex[:12]}"
    first = client.query("SELECT 42 AS v", job_id=job_id, job_retry=None)
    first.result()
    with pytest.raises(exceptions.Conflict):
        client.query("SELECT 42 AS v", job_id=job_id, job_retry=None)
    job = client.get_job(job_id)
    assert job.state == "DONE"
    assert [row.v for row in job.result()] == [42]


def test_bigquery_load_csv_from_file(client, dataset):
    ref = f"{PROJECT}.{dataset}.loaded"
    payload = (
        b"id,name,day,amount,active,note\n"
        b"1,ada,2024-01-02,1.5,true,\n"
        b"2,grace,2024-02-29,10.0,false,hi\n"
    )
    config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("id", "INTEGER"),
            bigquery.SchemaField("name", "STRING"),
            bigquery.SchemaField("day", "DATE"),
            bigquery.SchemaField("amount", "FLOAT"),
            bigquery.SchemaField("active", "BOOLEAN"),
            bigquery.SchemaField("note", "STRING"),
        ],
        source_format="CSV",
        skip_leading_rows=1,
        write_disposition="WRITE_TRUNCATE",
    )
    client.load_table_from_file(io.BytesIO(payload), ref, job_config=config).result()
    rows = list(
        client.query(
            f"SELECT id, name, day, amount, active, note FROM `{ref}` ORDER BY id"
        ).result()
    )
    assert [tuple(r.values()) for r in rows] == [
        (1, "ada", datetime.date(2024, 1, 2), 1.5, True, None),
        (2, "grace", datetime.date(2024, 2, 29), 10.0, False, "hi"),
    ]


def test_bigquery_load_table_from_json_rows(client, dataset):
    ref = f"{PROJECT}.{dataset}.loaded_json"
    rows = [
        {
            "id": 1,
            "day": "2024-01-02",
            "amount": 1.5,
            "active": True,
            "note": None,
            "tags": ["x", "y"],
            "point": {"x": 3},
        },
        {
            "id": 2,
            "day": "2024-02-29",
            "amount": 10,
            "active": False,
            "note": "hi",
            "tags": ["z"],
            "point": None,
        },
    ]
    config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("id", "INTEGER"),
            bigquery.SchemaField("day", "DATE"),
            bigquery.SchemaField("amount", "FLOAT"),
            bigquery.SchemaField("active", "BOOLEAN"),
            bigquery.SchemaField("note", "STRING"),
            bigquery.SchemaField("tags", "STRING", mode="REPEATED"),
            bigquery.SchemaField("point", "RECORD", fields=[bigquery.SchemaField("x", "INTEGER")]),
        ],
        write_disposition="WRITE_TRUNCATE",
    )
    client.load_table_from_json(rows, ref, job_config=config).result()
    loaded = client.query(
        f"SELECT id, day, amount, active, note, tags, point FROM `{ref}` ORDER BY id"
    ).result()
    assert [tuple(r.values()) for r in loaded] == [
        (1, datetime.date(2024, 1, 2), 1.5, True, None, ["x", "y"], {"x": 3}),
        (2, datetime.date(2024, 2, 29), 10.0, False, "hi", ["z"], None),
    ]


def test_bigquery_division_by_zero_is_bad_request(client):
    with pytest.raises(exceptions.BadRequest, match="division by zero"):
        client.query("SELECT 1/0 AS v").result()


def test_bigquery_missing_table_is_not_found(client, dataset):
    with pytest.raises(exceptions.NotFound):
        client.query(f"SELECT * FROM `{PROJECT}.{dataset}.missing`").result()


def test_bigquery_syntax_error_is_bad_request(client):
    with pytest.raises(exceptions.BadRequest, match="Syntax error"):
        client.query("SELEC 1").result()


def test_bigquery_concurrent_clients_return_correct_rows():
    def worker(n: int) -> list[int]:
        own = make_client()
        values = []
        for i in range(5):
            wanted = n * 100 + i
            rows = list(own.query(f"SELECT {wanted} AS v").result())
            values.append(rows[0].v)
        return values

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert results == [[n * 100 + i for i in range(5)] for n in range(8)]


def make_numbers(client: bigquery.Client, dataset: str) -> str:
    ref = f"{PROJECT}.{dataset}.numbers"
    schema = [bigquery.SchemaField("x", "INTEGER"), bigquery.SchemaField("s", "STRING")]
    client.create_table(bigquery.Table(ref, schema=schema))
    client.query(
        f"INSERT INTO `{ref}` (x, s) SELECT x, CONCAT('row', CAST(x AS STRING))"
        " FROM UNNEST(GENERATE_ARRAY(1, 25)) AS x"
    ).result()
    return ref


def test_bigquery_list_rows_with_max_results_and_start_index(client, dataset):
    ref = make_numbers(client, dataset)
    rows = list(client.list_rows(ref, max_results=5, start_index=3))
    assert [(row.x, row.s) for row in rows] == [(x, f"row{x}") for x in range(4, 9)]


def test_bigquery_list_rows_pages_with_page_size(client, dataset):
    ref = make_numbers(client, dataset)
    pages = list(client.list_rows(ref, page_size=10).pages)
    assert len(pages) == 3
    assert [row.x for page in pages for row in page] == list(range(1, 26))


def test_bigquery_insert_rows_and_insert_rows_json(client, dataset):
    """The streaming inserts Python test suites use to create fixture data (tabledata.insertAll)."""
    schema = [
        bigquery.SchemaField("id", "INTEGER"),
        bigquery.SchemaField("name", "STRING"),
        bigquery.SchemaField("day", "DATE"),
        bigquery.SchemaField("at", "TIMESTAMP"),
        bigquery.SchemaField("amount", "NUMERIC"),
        bigquery.SchemaField("tags", "STRING", mode="REPEATED"),
    ]
    table = client.create_table(bigquery.Table(f"{PROJECT}.{dataset}.streamed", schema=schema))
    at = datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC)
    rows = [(1, "ada", datetime.date(2024, 1, 2), at, decimal.Decimal("1.5"), ["a", "b"])]
    assert client.insert_rows(table, rows) == []
    assert client.insert_rows_json(table, [{"id": 2, "name": "grace", "tags": []}]) == []
    errors = client.insert_rows_json(table, [{"id": "abc"}])
    assert errors[0]["errors"][0]["reason"] == "invalid"
    got = list(client.query(f"SELECT * FROM `{table.full_table_id.replace(':', '.')}` ORDER BY id"))
    assert [tuple(r.values()) for r in got] == [
        (1, "ada", datetime.date(2024, 1, 2), at, decimal.Decimal("1.5"), ["a", "b"]),
        (2, "grace", None, None, None, []),
    ]


def test_bigquery_dry_run_returns_the_schema_and_zero_bytes(client):
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = client.query("SELECT 1 AS a, 'x' AS b", job_config=config)
    assert job.state == "DONE"
    assert job.total_bytes_processed == 0
    assert [(f.name, f.field_type) for f in job.schema] == [("a", "INTEGER"), ("b", "STRING")]


def test_bigquery_query_parameters(client):
    at = datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC)
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("i", "INT64", 5),
            bigquery.ScalarQueryParameter("s", "STRING", "it's"),
            bigquery.ScalarQueryParameter("n", "NUMERIC", decimal.Decimal("1.5")),
            bigquery.ScalarQueryParameter("ts", "TIMESTAMP", at),
            bigquery.ScalarQueryParameter("d", "DATE", datetime.date(2024, 2, 29)),
            bigquery.ArrayQueryParameter("a", "INT64", [1, 2, 3]),
            bigquery.StructQueryParameter("st", bigquery.ScalarQueryParameter("x", "INT64", 7)),
        ]
    )
    sql = (
        "SELECT @i + 1 AS i, @s AS s, @n AS n, @ts AS ts, @d AS d, ARRAY_LENGTH(@a) AS l, "
        "@st.x AS x"
    )
    [row] = list(client.query(sql, job_config=config).result())
    assert tuple(row.values()) == (
        6, "it's", decimal.Decimal("1.5"), at, datetime.date(2024, 2, 29), 3, 7,
    )  # fmt: skip
    positional = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter(None, "INT64", 2)]
    )
    assert next(iter(client.query("SELECT ? * 10 AS v", job_config=positional).result())).v == 20


def test_bigquery_copy_table_and_a_partition(client, dataset):
    """client.copy_table, as dbt-bigquery's insert_overwrite with copy_partitions uses it."""
    client.query(
        f"CREATE TABLE `{PROJECT}.{dataset}.m` PARTITION BY d AS "
        "SELECT DATE '2024-01-01' AS d, 1 AS k UNION ALL SELECT DATE '2024-01-02', 2"
    ).result()
    client.query(
        f"CREATE TABLE `{PROJECT}.{dataset}.tmp` PARTITION BY d AS "
        "SELECT DATE '2024-01-02' AS d, 20 AS k"
    ).result()
    truncate = bigquery.CopyJobConfig(write_disposition="WRITE_TRUNCATE")
    client.copy_table(
        f"{PROJECT}.{dataset}.tmp$20240102", f"{PROJECT}.{dataset}.m$20240102", job_config=truncate
    ).result()
    rows = client.query(f"SELECT k FROM `{PROJECT}.{dataset}.m` ORDER BY d").result()
    assert [r.k for r in rows] == [1, 20]
    client.copy_table(f"{PROJECT}.{dataset}.m", f"{PROJECT}.{dataset}.whole").result()
    assert client.get_table(f"{PROJECT}.{dataset}.whole").num_rows == 2


def test_bigquery_sql_udfs_and_routines(client, dataset):
    temp = "CREATE TEMP FUNCTION f(x INT64) AS (x * 3); SELECT f(2) AS v"
    assert next(iter(client.query(temp).result())).v == 6
    client.query(
        f"CREATE FUNCTION `{PROJECT}.{dataset}.g`(x FLOAT64) RETURNS FLOAT64 AS (x / 2)"
    ).result()
    assert next(iter(client.query(f"SELECT `{PROJECT}.{dataset}`.g(5) AS v").result())).v == 2.5
    [listed] = list(client.list_routines(f"{PROJECT}.{dataset}"))
    assert listed.routine_id == "g"
    routine = client.get_routine(f"{PROJECT}.{dataset}.g")
    assert routine.type_ == "SCALAR_FUNCTION"
    assert [a.name for a in routine.arguments] == ["x"]
    assert routine.body == "(x / 2)"
