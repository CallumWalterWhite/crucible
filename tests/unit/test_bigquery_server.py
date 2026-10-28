"""BigQuery REST server behaviour, one test per behaviour, with raw REST calls (no google client).

Field-by-field agreement with the recorded fixtures is tested by test_bigquery_fixtures.py; this
file tests what the endpoints do: paging, job lifecycle, errors, datasets, tables, uploads and
concurrency.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from crucible.engine import Engine
from crucible.server.app import create_app

PROJECT = "p"
API = f"/bigquery/v2/projects/{PROJECT}"
UPLOAD = f"/upload/bigquery/v2/projects/{PROJECT}/jobs"
ROWS_25 = "SELECT x FROM UNNEST(GENERATE_ARRAY(1, 25)) AS x ORDER BY x"


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = Engine()
    with TestClient(create_app("bigquery", engine), raise_server_exceptions=False) as c:
        yield c
    engine.close()


def make_dataset(client: TestClient, dataset: str = "ds", **body: Any) -> Any:
    payload = {"datasetReference": {"projectId": PROJECT, "datasetId": dataset}, **body}
    return client.post(f"{API}/datasets", json=payload)


def insert_query(
    client: TestClient,
    sql: str,
    *,
    job_id: str | None = None,
    location: str | None = None,
    configuration_extra: dict[str, Any] | None = None,
) -> Any:
    reference: dict[str, Any] = {}
    if job_id is not None:
        reference["jobId"] = job_id
    if location is not None:
        reference["location"] = location
    query: dict[str, Any] = {
        "query": sql,
        "useLegacySql": False,
        "defaultDataset": {"projectId": PROJECT, "datasetId": "ds"},
        **(configuration_extra or {}),
    }
    body: dict[str, Any] = {"configuration": {"query": query}}
    if reference:
        body["jobReference"] = reference
    return client.post(f"{API}/jobs", json=body)


def run_query(client: TestClient, sql: str, **body_extra: Any) -> Any:
    body: dict[str, Any] = {
        "query": sql,
        "useLegacySql": False,
        "defaultDataset": {"projectId": PROJECT, "datasetId": "ds"},
        **body_extra,
    }
    return client.post(f"{API}/queries", json=body)


def values(rows: list[dict[str, Any]]) -> list[str]:
    return [cell["v"] for row in rows for cell in row["f"]]


# --- 1. getQueryResults paging ----------------------------------------------------------------
def test_get_query_results_pages_through_25_rows_in_order(client):
    make_dataset(client)
    job = insert_query(client, ROWS_25, job_id="pages").json()
    assert job["status"]["state"] == "DONE"
    job_id = job["jobReference"]["jobId"]

    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"maxResults": 10}
        if token is not None:
            params["pageToken"] = token
        body = client.get(f"{API}/queries/{job_id}", params=params).json()
        pages += 1
        seen.extend(values(body["rows"]))
        token = body.get("pageToken")
        if token is None:
            break
        assert pages < 10, "paging did not end"
    assert pages == 3
    assert seen == [str(i) for i in range(1, 26)]
    assert body["totalRows"] == "25"
    assert len(body["rows"]) == 5  # the last page is the 5 remaining rows


def test_start_index_20_returns_the_last_5_rows_and_no_token(client):
    make_dataset(client)
    job_id = insert_query(client, ROWS_25, job_id="start").json()["jobReference"]["jobId"]
    body = client.get(f"{API}/queries/{job_id}", params={"startIndex": 20}).json()
    assert values(body["rows"]) == ["21", "22", "23", "24", "25"]
    assert "pageToken" not in body


def test_max_results_zero_gives_no_rows_and_a_token(client):
    make_dataset(client)
    job_id = insert_query(client, ROWS_25, job_id="zero").json()["jobReference"]["jobId"]
    body = client.get(f"{API}/queries/{job_id}", params={"maxResults": 0}).json()
    assert "rows" not in body
    assert body["pageToken"]
    assert body["totalRows"] == "25"


# --- 2. jobs.query paging and job creation ----------------------------------------------------
def test_jobs_query_pages_and_the_token_continues_through_get_query_results(client):
    make_dataset(client)
    body = run_query(client, ROWS_25.replace("25", "5"), maxResults=2).json()
    assert len(body["rows"]) == 2
    assert body["pageToken"]
    assert body["jobReference"]["jobId"]
    job_id = body["jobReference"]["jobId"]
    rest = client.get(
        f"{API}/queries/{job_id}", params={"pageToken": body["pageToken"], "maxResults": 10}
    ).json()
    assert values(rest["rows"]) == ["3", "4", "5"]


def test_job_creation_optional_with_a_one_row_result_has_no_job_reference(client):
    make_dataset(client)
    body = run_query(client, "SELECT 1 AS a", jobCreationMode="JOB_CREATION_OPTIONAL").json()
    assert "jobReference" not in body
    assert values(body["rows"]) == ["1"]


def test_job_creation_optional_with_a_paged_result_has_a_job_and_large_results(client):
    make_dataset(client)
    body = run_query(
        client, ROWS_25.replace("25", "5"), maxResults=2, jobCreationMode="JOB_CREATION_OPTIONAL"
    ).json()
    assert body["jobReference"]["jobId"]
    assert body["jobCreationReason"]["code"] == "LARGE_RESULTS"
    assert body["pageToken"]


# --- 3. duplicate job ids ---------------------------------------------------------------------
def test_a_duplicate_job_id_is_refused_and_the_first_job_is_kept(client):
    first = insert_query(client, "SELECT 1 AS first", job_id="dup")
    assert first.status_code == 200
    second = insert_query(client, "SELECT 2 AS second", job_id="dup")
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["errors"][0]["reason"] == "duplicate"
    assert error["status"] == "ALREADY_EXISTS"
    assert error["message"] == f"Already Exists: Job {PROJECT}:US.dup"
    kept = client.get(f"{API}/jobs/dup").json()
    assert kept["configuration"]["query"]["query"] == "SELECT 1 AS first"


# --- 4. location --------------------------------------------------------------------------------
def test_a_location_is_echoed_in_the_reference_the_id_and_jobs_get(client):
    make_dataset(client)
    job = insert_query(client, "SELECT 1 AS a", job_id="eu", location="EU").json()
    assert job["jobReference"]["location"] == "EU"
    assert job["id"] == f"{PROJECT}:EU.eu"
    fetched = client.get(f"{API}/jobs/eu", params={"location": "EU"}).json()
    assert fetched["jobReference"]["location"] == "EU"
    assert fetched["id"] == f"{PROJECT}:EU.eu"


# --- 5. failed statements ---------------------------------------------------------------------
def test_a_failed_select_is_a_failed_job_with_invalid_query(client):
    job = insert_query(client, "SELECT 1/0 AS v", job_id="div").json()
    assert job["status"]["errorResult"]["reason"] == "invalidQuery"
    assert job["status"]["errorResult"]["location"] == "query"


def test_get_query_results_of_a_failed_job_is_a_400_on_the_query_parameter(client):
    insert_query(client, "SELECT 1/0 AS v", job_id="div2")
    response = client.get(f"{API}/queries/div2")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["errors"][0]["location"] == "q"
    assert error["errors"][0]["reason"] == "invalidQuery"


def test_jobs_query_of_a_failed_select_is_a_400_on_the_query_parameter(client):
    response = run_query(client, "SELECT 1/0 AS v")
    assert response.status_code == 400
    assert response.json()["error"]["errors"][0]["location"] == "q"


def test_a_missing_table_is_a_not_found_job_error_naming_the_location(client):
    make_dataset(client)
    job = insert_query(client, "SELECT * FROM ds.missing", job_id="gone").json()
    error = job["status"]["errorResult"]
    assert error["reason"] == "notFound"
    assert "location" not in error
    assert error["message"].endswith("was not found in location US")


def test_a_missing_table_in_jobs_query_is_a_404(client):
    make_dataset(client)
    response = run_query(client, "SELECT * FROM ds.missing")
    assert response.status_code == 404
    assert response.json()["error"]["errors"][0]["reason"] == "notFound"


# --- 6. unsupported requests ------------------------------------------------------------------
def test_jobs_query_without_use_legacy_sql_is_unsupported(client):
    body = client.post(
        f"{API}/queries",
        json={
            "query": "SELECT 1 AS a",
            "defaultDataset": {"projectId": PROJECT, "datasetId": "ds"},
        },
    )
    assert body.status_code == 400
    error = body.json()["error"]["errors"][0]
    assert error["reason"] == "invalidQuery"
    assert error["message"].startswith("Not supported by Crucible")


def test_jobs_insert_without_use_legacy_sql_fails_the_job_with_the_same_message(client):
    # A failed job answers 200 with status.errorResult (ADR 0004), not an HTTP 400.
    body = client.post(
        f"{API}/jobs",
        json={"configuration": {"query": {"query": "SELECT 1 AS a"}}},
    ).json()
    error = body["status"]["errorResult"]
    assert error["reason"] == "invalidQuery"
    assert error["message"].startswith("Not supported by Crucible")


def test_jobs_query_with_query_parameters_is_unsupported(client):
    response = run_query(client, "SELECT @x AS x", queryParameters=[{"name": "x"}])
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


def dry_run(client: TestClient, sql: str, job_id: str = "dry") -> Any:
    query = {
        "query": sql,
        "useLegacySql": False,
        "defaultDataset": {"projectId": PROJECT, "datasetId": "ds"},
    }
    body = {"jobReference": {"jobId": job_id}, "configuration": {"dryRun": True, "query": query}}
    return client.post(f"{API}/jobs", json=body)


def test_a_dry_run_query_returns_its_schema_without_running(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64, b STRING)")
    job = dry_run(client, "SELECT a, b, 1 / 0 AS z FROM ds.t").json()
    assert job["status"] == {"state": "DONE"}
    assert job["configuration"]["dryRun"] is True
    stats = job["statistics"]["query"]
    assert stats["statementType"] == "SELECT"
    assert stats["totalBytesProcessed"] == "0"
    assert [(f["name"], f["type"]) for f in stats["schema"]["fields"]] == [
        ("a", "INTEGER"),
        ("b", "STRING"),
        ("z", "FLOAT"),
    ]
    assert client.get(f"{API}/jobs/dry").status_code == 404  # a dry run is not stored


def test_a_dry_run_statement_changes_nothing(client):
    make_dataset(client)
    assert (
        dry_run(client, "CREATE TABLE ds.u (a INT64)").json()["statistics"]["query"][
            "statementType"
        ]
        == "CREATE_TABLE"
    )
    assert client.get(f"{API}/datasets/ds/tables/u").status_code == 404


def test_a_dry_run_reports_plan_time_errors(client):
    make_dataset(client)
    response = dry_run(client, "SELECT * FROM ds.missing")
    # An HTTP error, not a failed job (jobs_insert_dry_run_missing_table).
    assert response.status_code == 404
    assert response.json()["error"]["errors"][0]["reason"] == "notFound"


def test_jobs_query_dry_run_has_a_schema_and_no_rows_or_job_id(client):
    make_dataset(client)
    body = run_query(client, "SELECT 1 AS a", dryRun=True).json()
    assert body["schema"]["fields"][0]["name"] == "a"
    assert "rows" not in body
    assert "jobId" not in body["jobReference"]
    assert body["totalBytesProcessed"] == "0"


# --- 7. DDL and DML jobs ----------------------------------------------------------------------
def test_create_table_as_select_reports_its_statement_and_target(client):
    make_dataset(client)
    job = insert_query(client, "CREATE TABLE ds.t AS SELECT 1 AS a, 'x' AS b", job_id="ctas").json()
    stats = job["statistics"]["query"]
    assert stats["statementType"] == "CREATE_TABLE_AS_SELECT"
    assert stats["ddlTargetTable"] == {"projectId": PROJECT, "datasetId": "ds", "tableId": "t"}
    assert [f["name"] for f in stats["schema"]["fields"]] == ["a", "b"]


def test_a_ddl_job_has_no_total_rows_and_no_schema_in_its_results(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t AS SELECT 1 AS a", job_id="ctas2")
    body = client.get(f"{API}/queries/ctas2").json()
    assert "totalRows" not in body
    assert "schema" not in body


def test_an_insert_job_reports_the_affected_rows(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    job = insert_query(client, "INSERT INTO ds.t VALUES (1), (2)", job_id="ins").json()
    stats = job["statistics"]["query"]
    assert stats["numDmlAffectedRows"] == "2"
    assert stats["dmlStats"]["insertedRowCount"] == "2"
    body = client.get(f"{API}/queries/ins").json()
    assert body["numDmlAffectedRows"] == "2"


@pytest.mark.parametrize(
    ("sql", "statement", "counter", "count"),
    [
        ("UPDATE ds.t SET a = 5 WHERE a > 1", "UPDATE", "updatedRowCount", "2"),
        ("DELETE FROM ds.t WHERE a = 1", "DELETE", "deletedRowCount", "1"),
    ],
)
def test_update_and_delete_jobs_report_the_affected_rows(client, sql, statement, counter, count):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    insert_query(client, "INSERT INTO ds.t VALUES (1), (2), (3)")
    stats = insert_query(client, sql).json()["statistics"]["query"]
    assert stats["statementType"] == statement
    assert stats["numDmlAffectedRows"] == count
    assert stats["dmlStats"] == {counter: count}


# --- 8. anonymous result tables ---------------------------------------------------------------
def test_the_anonymous_destination_of_a_select_is_a_table_with_nullable_fields(client):
    make_dataset(client)
    job = insert_query(client, "SELECT 1 AS a, 'x' AS b", job_id="anon").json()
    destination = job["configuration"]["query"]["destinationTable"]
    table = client.get(
        f"{API}/datasets/{destination['datasetId']}/tables/{destination['tableId']}"
    ).json()
    assert [f["mode"] for f in table["schema"]["fields"]] == ["NULLABLE", "NULLABLE"]
    assert table["numRows"] == "1"


def test_an_unknown_anonymous_table_is_a_404(client):
    response = client.get(f"{API}/datasets/_crucible_anonymous/tables/anonmissing")
    assert response.status_code == 404


# --- 9. datasets -------------------------------------------------------------------------------
def test_dataset_labels_and_location_are_echoed_by_get(client):
    labels = {"team": "data"}
    created = make_dataset(client, labels=labels, location="EU").json()
    assert created["labels"] == labels
    assert created["location"] == "EU"
    fetched = client.get(f"{API}/datasets/ds").json()
    assert fetched["labels"] == labels
    assert fetched["location"] == "EU"


def test_datasets_list_filters_on_a_label_key_and_value(client):
    make_dataset(client, "a", labels={"k": "v"})
    make_dataset(client, "b", labels={"k": "other"})
    listed = client.get(f"{API}/datasets", params={"filter": "labels.k:v"}).json()
    assert [d["datasetReference"]["datasetId"] for d in listed["datasets"]] == ["a"]
    other = client.get(f"{API}/datasets", params={"filter": "labels.k:other"}).json()
    assert [d["datasetReference"]["datasetId"] for d in other["datasets"]] == ["b"]


def test_a_malformed_dataset_filter_is_unsupported(client):
    response = client.get(f"{API}/datasets", params={"filter": "foo"})
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


def test_inserting_a_dataset_twice_is_a_duplicate(client):
    assert make_dataset(client).status_code == 200
    again = make_dataset(client)
    assert again.status_code == 409
    assert again.json()["error"]["errors"][0]["reason"] == "duplicate"


def test_deleting_a_non_empty_dataset_without_delete_contents_is_unsupported(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    response = client.delete(f"{API}/datasets/ds")
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


def test_deleting_a_dataset_with_delete_contents_removes_it(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    assert client.delete(f"{API}/datasets/ds", params={"deleteContents": "true"}).status_code == 204
    assert client.get(f"{API}/datasets/ds").status_code == 404


# --- 10. tables --------------------------------------------------------------------------------
REST_SCHEMA = {
    "fields": [
        {"name": "i", "type": "INTEGER", "mode": "NULLABLE"},
        {"name": "s", "type": "STRING", "mode": "NULLABLE"},
        {"name": "b", "type": "BOOLEAN", "mode": "NULLABLE"},
        {"name": "f", "type": "FLOAT", "mode": "NULLABLE"},
        {"name": "n", "type": "NUMERIC", "mode": "NULLABLE"},
        {"name": "ts", "type": "TIMESTAMP", "mode": "NULLABLE"},
        {"name": "d", "type": "DATE", "mode": "NULLABLE"},
        {"name": "dt", "type": "DATETIME", "mode": "NULLABLE"},
        {"name": "t", "type": "TIME", "mode": "NULLABLE"},
        {"name": "bs", "type": "BYTES", "mode": "NULLABLE"},
        {"name": "js", "type": "JSON", "mode": "NULLABLE"},
        {"name": "tags", "type": "INTEGER", "mode": "REPEATED"},
        {
            "name": "rec",
            "type": "RECORD",
            "mode": "NULLABLE",
            "fields": [{"name": "a", "type": "INTEGER", "mode": "NULLABLE"}],
        },
    ]
}


def make_table(client: TestClient, table: str = "t", schema: dict[str, Any] | None = None) -> Any:
    return client.post(
        f"{API}/datasets/ds/tables",
        json={
            "tableReference": {"projectId": PROJECT, "datasetId": "ds", "tableId": table},
            "schema": schema or REST_SCHEMA,
        },
    )


def make_table_with(client: TestClient, schema: dict[str, Any], **metadata: Any) -> Any:
    return client.post(
        f"{API}/datasets/ds/tables",
        json={
            "tableReference": {"projectId": PROJECT, "datasetId": "ds", "tableId": "t"},
            "schema": schema,
            **metadata,
        },
    )


def test_a_table_round_trips_its_rest_schema_without_nullable_modes(client):
    make_dataset(client)
    assert make_table(client).status_code == 200
    got = client.get(f"{API}/datasets/ds/tables/t").json()
    fields = got["schema"]["fields"]
    assert [f["name"] for f in fields] == [f["name"] for f in REST_SCHEMA["fields"]]
    assert [f["type"] for f in fields] == [f["type"] for f in REST_SCHEMA["fields"]]
    assert all("mode" not in f for f in fields if f["name"] != "tags")
    assert [f["mode"] for f in fields if f["name"] == "tags"] == ["REPEATED"]
    assert got["numRows"] == "0"


def test_inserting_a_table_twice_is_a_duplicate(client):
    make_dataset(client)
    make_table(client)
    again = make_table(client)
    assert again.status_code == 409
    assert again.json()["error"]["errors"][0]["reason"] == "duplicate"


def test_tables_list_pages_with_next_page_token_and_reports_the_total(client):
    make_dataset(client)
    for name in ("a", "b", "c"):
        make_table(client, name, {"fields": [{"name": "x", "type": "INTEGER"}]})
    first = client.get(f"{API}/datasets/ds/tables", params={"maxResults": 1}).json()
    assert [t["tableReference"]["tableId"] for t in first["tables"]] == ["a"]
    assert first["totalItems"] == 3
    second = client.get(
        f"{API}/datasets/ds/tables",
        params={"maxResults": 1, "pageToken": first["nextPageToken"]},
    ).json()
    assert [t["tableReference"]["tableId"] for t in second["tables"]] == ["b"]


def test_patch_appends_a_column_that_is_then_queryable(client):
    make_dataset(client)
    make_table(client, "t", {"fields": [{"name": "x", "type": "INTEGER"}]})
    insert_query(client, "INSERT INTO ds.t (x) VALUES (1)")
    patched = client.patch(
        f"{API}/datasets/ds/tables/t",
        json={
            "schema": {
                "fields": [{"name": "x", "type": "INTEGER"}, {"name": "y", "type": "STRING"}]
            }
        },
    )
    assert patched.status_code == 200
    assert [f["name"] for f in patched.json()["schema"]["fields"]] == ["x", "y"]
    rows = run_query(client, "SELECT x, y FROM ds.t").json()["rows"]
    assert values(rows) == ["1", None]


def test_patch_that_changes_a_column_type_is_unsupported(client):
    make_dataset(client)
    make_table(client, "t", {"fields": [{"name": "x", "type": "INTEGER"}]})
    response = client.patch(
        f"{API}/datasets/ds/tables/t",
        json={"schema": {"fields": [{"name": "x", "type": "STRING"}]}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


def test_deleting_a_table_removes_it(client):
    make_dataset(client)
    make_table(client)
    assert client.delete(f"{API}/datasets/ds/tables/t").status_code == 204
    assert client.get(f"{API}/datasets/ds/tables/t").status_code == 404


def test_a_view_lists_as_a_view_and_deletes_through_tables_delete(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    insert_query(client, "CREATE VIEW ds.v AS SELECT a FROM ds.t")
    listed = client.get(f"{API}/datasets/ds/tables").json()
    kinds = {t["tableReference"]["tableId"]: t["type"] for t in listed["tables"]}
    assert kinds["v"] == "VIEW"
    assert kinds["t"] == "TABLE"
    assert client.get(f"{API}/datasets/ds/tables/v").json()["type"] == "VIEW"
    assert client.delete(f"{API}/datasets/ds/tables/v").status_code == 204
    assert client.get(f"{API}/datasets/ds/tables/v").status_code == 404


def test_description_labels_and_field_descriptions_are_stored_and_returned(client):
    make_dataset(client)
    schema = {
        "fields": [
            {"name": "k", "type": "INTEGER", "mode": "REQUIRED", "description": "the key"},
            {"name": "s", "type": "STRING", "description": "the label"},
        ]
    }
    created = make_table_with(client, schema, description="a table", labels={"team": "data"})
    assert created.status_code == 200
    got = client.get(f"{API}/datasets/ds/tables/t").json()
    assert got["description"] == "a table"
    assert got["labels"] == {"team": "data"}
    assert got["schema"]["fields"] == [
        {"name": "k", "type": "INTEGER", "mode": "REQUIRED", "description": "the key"},
        {"name": "s", "type": "STRING", "description": "the label"},
    ]
    refused = run_query(client, "INSERT INTO ds.t (k, s) VALUES (NULL, 'x')")
    assert refused.status_code == 400
    assert "cannot be null" in refused.json()["error"]["message"]


def test_patch_sets_description_labels_and_field_descriptions(client):
    make_dataset(client)
    make_table(client, "t", {"fields": [{"name": "x", "type": "INTEGER"}]})
    described = client.patch(
        f"{API}/datasets/ds/tables/t",
        json={
            "description": "new",
            "labels": {"env": "dev", "gone": None},
            "schema": {
                "fields": [
                    {
                        "name": "x",
                        "type": "INTEGER",
                        "description": "col",
                        "policyTags": {"names": []},
                    }
                ]
            },
        },
    ).json()
    assert described["description"] == "new"
    assert described["labels"] == {"env": "dev"}
    assert described["schema"]["fields"] == [{"name": "x", "type": "INTEGER", "description": "col"}]
    kept = client.patch(f"{API}/datasets/ds/tables/t", json={"labels": {}}).json()
    assert kept["description"] == "new"
    assert "labels" not in kept
    cleared = client.patch(
        f"{API}/datasets/ds/tables/t",
        json={"description": "", "schema": {"fields": [{"name": "x", "type": "INTEGER"}]}},
    ).json()
    assert "description" not in cleared
    assert "description" not in cleared["schema"]["fields"][0]


@pytest.mark.parametrize(
    "fields",
    [
        [{"name": "x", "type": "INTEGER", "mode": "REQUIRED"}],  # a relaxed or tightened mode
        [{"name": "x", "type": "INTEGER", "policyTags": {"names": ["p"]}}],
        [{"name": "x", "type": "INTEGER"}, {"name": "y", "type": "INT64", "mode": "REQUIRED"}],
    ],
)
def test_patch_that_changes_a_mode_or_adds_a_required_column_is_unsupported(client, fields):
    make_dataset(client)
    make_table(client, "t", {"fields": [{"name": "x", "type": "INTEGER"}]})
    response = client.patch(f"{API}/datasets/ds/tables/t", json={"schema": {"fields": fields}})
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


def test_a_description_inside_a_record_is_unsupported(client):
    make_dataset(client)
    schema = {
        "fields": [
            {
                "name": "rec",
                "type": "RECORD",
                "fields": [{"name": "a", "type": "INTEGER", "description": "inner"}],
            }
        ]
    }
    response = make_table(client, "t", schema)
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


def test_alter_statements_set_options_that_tables_get_returns(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    insert_query(client, "CREATE VIEW ds.v AS SELECT a FROM ds.t")
    run_query(client, "ALTER TABLE ds.t SET OPTIONS (description = 'table', labels = [('k', 'v')])")
    run_query(client, "ALTER VIEW ds.v SET OPTIONS (description = 'view')")
    run_query(client, "ALTER TABLE ds.t ALTER COLUMN a SET OPTIONS (description = 'col')")
    table = client.get(f"{API}/datasets/ds/tables/t").json()
    assert table["description"] == "table"
    assert table["labels"] == {"k": "v"}
    assert table["schema"]["fields"] == [{"name": "a", "type": "INTEGER", "description": "col"}]
    assert client.get(f"{API}/datasets/ds/tables/v").json()["description"] == "view"


# --- 11. routines ------------------------------------------------------------------------------
def test_routines_list_is_empty_on_an_existing_dataset(client):
    make_dataset(client)
    assert client.get(f"{API}/datasets/ds/routines").json() == {}


def test_routines_list_on_a_missing_dataset_is_a_404(client):
    assert client.get(f"{API}/datasets/nope/routines").status_code == 404


def test_routines_get_is_a_404(client):
    make_dataset(client)
    assert client.get(f"{API}/datasets/ds/routines/f").status_code == 404


# --- 12. resumable upload ----------------------------------------------------------------------
LOAD_FIELDS = [{"name": "id", "type": "INTEGER"}, {"name": "s", "type": "STRING"}]
CSV = b"id,s\n1,a\n2,b\n3,c\n"


def initiate(client: TestClient, table: str = "loaded", **config: Any) -> Any:
    body = {
        "jobReference": {"projectId": PROJECT, "jobId": f"load_{table}_{uuid.uuid4().hex[:8]}"},
        "configuration": {
            "load": {
                "destinationTable": {"projectId": PROJECT, "datasetId": "ds", "tableId": table},
                "schema": {"fields": LOAD_FIELDS},
                "sourceFormat": "CSV",
                "skipLeadingRows": 1,
                **config,
            }
        },
    }
    return client.post(f"{UPLOAD}", params={"uploadType": "resumable"}, json=body)


def upload_url(initiated: Any) -> str:
    location = initiated.headers["Location"]
    return location.removeprefix("http://testserver")


def test_a_resumable_upload_loads_the_rows_in_two_chunks(client):
    make_dataset(client)
    initiated = initiate(client)
    assert initiated.status_code == 200
    url = upload_url(initiated)
    first, rest = CSV[:10], CSV[10:]
    total = len(CSV)
    part = client.put(url, content=first, headers={"Content-Range": f"bytes 0-{len(first) - 1}/*"})
    assert part.status_code == 308
    assert part.headers["Range"] == f"bytes=0-{len(first) - 1}"
    done = client.put(
        url, content=rest, headers={"Content-Range": f"bytes {len(first)}-{total - 1}/{total}"}
    )
    assert done.status_code == 200
    assert done.json()["status"]["state"] == "DONE"
    rows = run_query(client, "SELECT id, s FROM ds.loaded ORDER BY id").json()["rows"]
    assert values(rows) == ["1", "a", "2", "b", "3", "c"]


def test_a_chunk_that_does_not_continue_the_upload_is_a_400(client):
    make_dataset(client)
    url = upload_url(initiate(client))
    client.put(url, content=b"id,s\n", headers={"Content-Range": "bytes 0-4/*"})
    response = client.put(url, content=b"x", headers={"Content-Range": "bytes 9-9/10"})
    assert response.status_code == 400


def test_a_chunk_whose_length_differs_from_its_content_range_is_a_400(client):
    make_dataset(client)
    url = upload_url(initiate(client))
    response = client.put(url, content=b"id,s\n1,a\n", headers={"Content-Range": "bytes 0-4/*"})
    assert response.status_code == 400


def test_an_unknown_upload_id_is_a_404(client):
    response = client.put(
        f"{UPLOAD}?uploadType=resumable&upload_id=nope",
        content=b"x",
        headers={"Content-Range": "bytes 0-0/1"},
    )
    assert response.status_code == 404


def test_an_upload_type_other_than_resumable_or_multipart_is_unsupported(client):
    response = client.post(f"{UPLOAD}", params={"uploadType": "media"}, json={})
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


BOUNDARY = "b0undary"


def multipart(client: TestClient, metadata: bytes, media: bytes, **kwargs: Any) -> Any:
    """The request cloud.google.com/go/bigquery v1.72.0 sends (captured from dbt Fusion): the
    job as JSON with a trailing newline, then the file, as multipart/related."""
    body = (
        f"--{BOUNDARY}\r\nContent-Type: application/json\r\n\r\n".encode()
        + metadata
        + b"\n"
        + f"\r\n--{BOUNDARY}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n".encode()
        + media
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    headers = {"Content-Type": f"multipart/related; boundary={BOUNDARY}"}
    return client.post(
        UPLOAD, params={"uploadType": "multipart"}, content=body, headers=headers | kwargs
    )


def load_metadata(table: str, job_id: str | None = None, **config: Any) -> bytes:
    load = {
        "destinationTable": {"projectId": PROJECT, "datasetId": "ds", "tableId": table},
        "fieldDelimiter": ",",
        "schema": {"fields": LOAD_FIELDS},
        "skipLeadingRows": 1,
        "sourceFormat": "CSV",
        **config,
    }
    job = {"jobId": job_id or f"mp_{uuid.uuid4().hex[:8]}", "location": "US", "projectId": PROJECT}
    return json.dumps({"configuration": {"load": load}, "jobReference": job}).encode()


def test_a_multipart_upload_loads_the_file_in_one_request(client):
    make_dataset(client)
    response = multipart(client, load_metadata("mp"), CSV.replace(b"\n", b"\r\n"))
    assert response.status_code == 200
    assert response.json()["status"]["state"] == "DONE"
    assert response.json()["configuration"]["jobType"] == "LOAD"
    rows = run_query(client, "SELECT id, s FROM ds.mp ORDER BY id").json()["rows"]
    assert values(rows) == ["1", "a", "2", "b", "3", "c"]


def test_a_rejected_multipart_file_fails_the_job_and_creates_no_table(client):
    make_dataset(client)
    schema = {"fields": [{"name": "b", "type": "BOOLEAN"}]}  # "maybe" is a recorded rejection
    job = multipart(client, load_metadata("bad", schema=schema), b"b\nmaybe\n").json()
    assert job["status"]["errorResult"]["reason"] == "invalid"
    assert (
        run_query(client, "SELECT * FROM ds.bad").json()["error"]["errors"][0]["reason"]
        == "notFound"
    )


def test_a_multipart_job_id_is_reserved_like_any_other(client):
    make_dataset(client)
    assert multipart(client, load_metadata("a", "same"), CSV).status_code == 200
    again = multipart(client, load_metadata("b", "same"), CSV)
    assert again.status_code == 409
    assert again.json()["error"]["errors"][0]["reason"] == "duplicate"


@pytest.mark.parametrize(
    "body",
    [
        b"not multipart at all",
        f"--{BOUNDARY}\r\nContent-Type: application/json\r\n\r\n{{}}\r\n--{BOUNDARY}--\r\n".encode(),  # noqa: E501
        (
            f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nx\r\n"
            f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\ny\r\n--{BOUNDARY}--\r\n"
        ).encode(),
    ],
    ids=["no parts", "one part", "metadata is not JSON"],
)
def test_a_malformed_multipart_body_is_refused(client, body):
    response = client.post(
        UPLOAD,
        params={"uploadType": "multipart"},
        content=body,
        headers={"Content-Type": f"multipart/related; boundary={BOUNDARY}"},
    )
    assert response.status_code == 400


def test_a_multipart_upload_without_multipart_related_is_refused(client):
    response = client.post(UPLOAD, params={"uploadType": "multipart"}, json={})
    assert response.status_code == 400


def load_csv(client: TestClient, table: str, data: bytes, **config: Any) -> dict[str, Any]:
    url = upload_url(initiate(client, table, **config))
    response = client.put(
        url, content=data, headers={"Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"}
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_write_append_adds_rows_to_an_existing_table(client):
    make_dataset(client)
    load_csv(client, "loaded", CSV)
    load_csv(client, "loaded", CSV, writeDisposition="WRITE_APPEND")
    rows = run_query(client, "SELECT COUNT(*) AS n FROM ds.loaded").json()["rows"]
    assert values(rows) == ["6"]


def test_write_append_with_a_different_schema_fails_the_job(client):
    make_dataset(client)
    load_csv(client, "loaded", CSV)
    other = b"id\n1\n"
    url = upload_url(
        client.post(
            UPLOAD,
            params={"uploadType": "resumable"},
            json={
                "jobReference": {"projectId": PROJECT, "jobId": "other"},
                "configuration": {
                    "load": {
                        "destinationTable": {
                            "projectId": PROJECT,
                            "datasetId": "ds",
                            "tableId": "loaded",
                        },
                        "schema": {"fields": [{"name": "id", "type": "INTEGER"}]},
                        "sourceFormat": "CSV",
                        "skipLeadingRows": 1,
                        "writeDisposition": "WRITE_APPEND",
                    }
                },
            },
        )
    )
    job = client.put(
        url, content=other, headers={"Content-Range": f"bytes 0-{len(other) - 1}/{len(other)}"}
    ).json()
    error = job["status"]["errorResult"]
    assert error["reason"] == "invalidQuery"
    assert error["message"].startswith("Not supported by Crucible")


def test_a_rejected_csv_fails_the_job_and_leaves_the_table_unchanged(client):
    make_dataset(client)
    load_csv(client, "loaded", CSV)
    bad = b"id,s\n1,2024-01-02\n"  # a date alone is not a TIMESTAMP (load_empty_fields_jobs_get)
    url = upload_url(
        client.post(
            UPLOAD,
            params={"uploadType": "resumable"},
            json={
                "jobReference": {"projectId": PROJECT, "jobId": "bad"},
                "configuration": {
                    "load": {
                        "destinationTable": {
                            "projectId": PROJECT,
                            "datasetId": "ds",
                            "tableId": "loaded",
                        },
                        "schema": {
                            "fields": [
                                {"name": "id", "type": "INTEGER"},
                                {"name": "s", "type": "TIMESTAMP"},
                            ]
                        },
                        "sourceFormat": "CSV",
                        "skipLeadingRows": 1,
                        "writeDisposition": "WRITE_TRUNCATE",
                    }
                },
            },
        )
    )
    job = client.put(
        url, content=bad, headers={"Content-Range": f"bytes 0-{len(bad) - 1}/{len(bad)}"}
    ).json()
    assert job["status"]["errorResult"]["reason"] == "invalid"
    rows = run_query(client, "SELECT COUNT(*) AS n FROM ds.loaded").json()["rows"]
    assert values(rows) == ["3"]  # the old rows are kept


def load_ndjson(
    client: TestClient,
    table: str,
    data: bytes,
    fields: list[dict[str, Any]] = LOAD_FIELDS,
    **config: Any,
) -> dict[str, Any]:
    """A resumable NEWLINE_DELIMITED_JSON load into ds.<table>, answered with the job resource."""
    body = {
        "jobReference": {"projectId": PROJECT, "jobId": f"ndjson_{uuid.uuid4().hex[:8]}"},
        "configuration": {
            "load": {
                "destinationTable": {"projectId": PROJECT, "datasetId": "ds", "tableId": table},
                "schema": {"fields": fields},
                "sourceFormat": "NEWLINE_DELIMITED_JSON",
                "writeDisposition": "WRITE_TRUNCATE",
                **config,
            }
        },
    }
    url = upload_url(client.post(UPLOAD, params={"uploadType": "resumable"}, json=body))
    response = client.put(
        url, content=data, headers={"Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"}
    )
    assert response.status_code == 200
    job: dict[str, Any] = response.json()
    return job


def test_an_ndjson_load_loads_the_rows_in_the_client_spellings(client):
    make_dataset(client)
    data = b'{"id": 1, "s": "a"}\n{"id": "2", "s": "b"}'  # no final newline, as the client sends
    job = load_ndjson(client, "nd", data)
    assert job["status"]["state"] == "DONE"
    assert "errorResult" not in job["status"]
    rows = run_query(client, "SELECT id, s FROM ds.nd ORDER BY id").json()["rows"]
    assert values(rows) == ["1", "a", "2", "b"]


def test_an_ndjson_load_keeps_repeated_and_record_fields(client):
    make_dataset(client)
    fields = [
        {"name": "a", "type": "INTEGER", "mode": "REPEATED"},
        {"name": "r", "type": "RECORD", "fields": [{"name": "x", "type": "INTEGER"}]},
    ]
    job = load_ndjson(client, "nested", b'{"a": [1, 2], "r": {"x": 3}}', fields)
    assert job["status"]["state"] == "DONE"
    rows = run_query(client, "SELECT ARRAY_LENGTH(a) AS n, r.x AS x FROM ds.nested").json()["rows"]
    assert values(rows) == ["2", "3"]


def test_an_ndjson_value_outside_the_confirmed_spellings_fails_the_job_as_unsupported(client):
    make_dataset(client)
    job = load_ndjson(client, "bad", b'{"id": "abc", "s": "x"}\n{"id": 2, "s": "y"}')
    error = job["status"]["errorResult"]
    assert error["reason"] == "invalidQuery"
    assert error["message"].startswith("Not supported by Crucible")
    missing = run_query(client, "SELECT * FROM ds.bad").json()
    assert missing["error"]["errors"][0]["reason"] == "notFound"  # no table was created


def test_an_unknown_ndjson_field_fails_unless_ignore_unknown_values_is_set(client):
    make_dataset(client)
    data = b'{"id": 1, "s": "a", "nope": 2}'
    refused = load_ndjson(client, "unknown", data)
    # BigQuery's three messages (ndjson_unknown_field_jobs_get).
    assert refused["status"]["errorResult"]["reason"] == "invalid"
    assert refused["status"]["errors"][-1]["message"].endswith(
        "JSON parsing error in row starting at position 0: No such field: nope."
    )
    loaded = load_ndjson(client, "ignored", data, ignoreUnknownValues=True)
    assert loaded["status"]["state"] == "DONE"
    rows = run_query(client, "SELECT id, s FROM ds.ignored").json()["rows"]
    assert values(rows) == ["1", "a"]


def test_an_ndjson_load_refuses_csv_only_options(client):
    make_dataset(client)
    job = load_ndjson(client, "csv_only", b'{"id": 1, "s": "a"}', skipLeadingRows=1)
    assert job["status"]["errorResult"]["message"] == (
        "Not supported by Crucible: load job configuration field skipLeadingRows"
    )


def test_an_ndjson_write_append_adds_rows_to_the_table(client):
    make_dataset(client)
    load_ndjson(client, "appended", b'{"id": 1, "s": "a"}')
    load_ndjson(client, "appended", b'{"id": 2, "s": "b"}', writeDisposition="WRITE_APPEND")
    rows = run_query(client, "SELECT COUNT(*) AS n FROM ds.appended").json()["rows"]
    assert values(rows) == ["2"]


# --- 13. concurrency ----------------------------------------------------------------------------
def test_concurrent_queries_and_ctas_through_one_client_all_succeed(client):
    make_dataset(client)
    failures: list[str] = []

    def worker(index: int) -> None:
        try:
            for n in range(10):
                body = insert_query(client, f"SELECT {index * 100 + n} AS v").json()
                if body["status"].get("errorResult"):
                    failures.append(f"select {index}/{n}: {body['status']['errorResult']}")
                    continue
                rows = client.get(f"{API}/queries/{body['jobReference']['jobId']}").json()["rows"]
                if values(rows) != [str(index * 100 + n)]:
                    failures.append(f"select {index}/{n}: {values(rows)}")
            for n in range(2):
                body = insert_query(
                    client, f"CREATE TABLE ds.w{index}_{n} AS SELECT {index} AS i"
                ).json()
                if body["status"].get("errorResult"):
                    failures.append(f"ctas {index}/{n}: {body['status']['errorResult']}")
        except Exception as exc:  # reported below, so a crashing thread is not silent
            failures.append(f"worker {index}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    listed = client.get(f"{API}/datasets/ds/tables", params={"maxResults": 100}).json()
    assert listed["totalItems"] == 16


# --- 14. reset ----------------------------------------------------------------------------------
def test_reset_clears_jobs_and_uploads(client):
    make_dataset(client)
    insert_query(client, "SELECT 1 AS a", job_id="before")
    url = upload_url(initiate(client, "pending"))
    assert client.post("/_crucible/reset").status_code == 200
    assert client.get(f"{API}/jobs/before").status_code == 404
    response = client.put(url, content=b"id,s\n", headers={"Content-Range": "bytes 0-4/*"})
    assert response.status_code == 404


# --- 15. tabledata.list ------------------------------------------------------------------------
def list_data(client: TestClient, table: str = "big", dataset: str = "ds", **params: Any) -> Any:
    return client.get(f"{API}/datasets/{dataset}/tables/{table}/data", params=params)


def make_big_table(client: TestClient) -> None:
    make_dataset(client)
    assert insert_query(client, f"CREATE TABLE ds.big AS {ROWS_25}").json()["status"]["state"] == (
        "DONE"
    )


def test_tabledata_list_pages_through_25_rows_in_order(client):
    make_big_table(client)
    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"maxResults": 10}
        if token is not None:
            params["pageToken"] = token
        body = list_data(client, **params).json()
        pages += 1
        seen.extend(values(body["rows"]))
        token = body.get("pageToken")
        if token is None:
            break
        assert pages < 10, "paging did not end"
    assert pages == 3
    assert seen == [str(i) for i in range(1, 26)]
    assert body["kind"] == "bigquery#tableDataList"
    assert body["totalRows"] == "25"
    assert len(body["rows"]) == 5


def test_tabledata_list_start_index_20_returns_the_last_5_rows_and_no_token(client):
    make_big_table(client)
    body = list_data(client, startIndex=20).json()
    assert values(body["rows"]) == ["21", "22", "23", "24", "25"]
    assert "pageToken" not in body
    assert body["totalRows"] == "25"


def test_tabledata_list_max_results_zero_gives_no_rows_and_a_token(client):
    make_big_table(client)
    body = list_data(client, maxResults=0).json()
    assert "rows" not in body
    assert body["pageToken"]
    assert body["totalRows"] == "25"


def test_tabledata_list_past_the_end_gives_no_rows_and_no_token(client):
    make_big_table(client)
    body = list_data(client, startIndex=99).json()
    assert "rows" not in body
    assert "pageToken" not in body
    assert body["totalRows"] == "25"


def test_tabledata_list_of_an_empty_table_has_total_rows_zero(client):
    make_dataset(client)
    make_table(client, "t")
    body = list_data(client, table="t").json()
    assert body == {
        "kind": "bigquery#tableDataList",
        "etag": body["etag"],
        "totalRows": "0",
    }


def test_tabledata_list_timestamps_are_float_seconds_unless_int64_is_asked_for(client):
    # tabledata.list writes float seconds, not getQueryResults' "1.704164645123456E9"
    # (tabledata_list: "1704164645.123456"); the int64 form is tabledata_list_int64_timestamp.
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.ts (t TIMESTAMP)")
    insert_query(client, "INSERT INTO ds.ts VALUES (TIMESTAMP '2024-01-02 03:04:05.123456 UTC')")
    default = list_data(client, table="ts").json()["rows"]
    assert values(default) == ["1704164645.123456"]
    for flag in ("true", "True"):
        int64 = list_data(client, table="ts", **{"formatOptions.useInt64Timestamp": flag}).json()
        assert values(int64["rows"]) == ["1704164645123456"]


def test_a_whole_second_timestamp_in_float_form_is_unsupported_and_int64_loads(client):
    # The float form of a whole second is unrecorded (encoding._tabledata_seconds): refused, not
    # guessed. The int64 form is the one the clients ask for.
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.ts (t TIMESTAMP)")
    insert_query(client, "INSERT INTO ds.ts VALUES (TIMESTAMP '2024-01-02 03:04:05 UTC')")
    response = list_data(client, table="ts")
    assert response.status_code == 400
    assert "tabledata.list float TIMESTAMP" in response.json()["error"]["message"]
    int64 = list_data(client, table="ts", **{"formatOptions.useInt64Timestamp": "true"}).json()
    assert values(int64["rows"]) == ["1704164645000000"]


def test_tabledata_list_of_an_anonymous_table_reads_the_job_result(client):
    make_dataset(client)
    job = insert_query(client, "SELECT 1 AS a, 'x' AS b", job_id="anonrows").json()
    destination = job["configuration"]["query"]["destinationTable"]
    body = client.get(
        f"{API}/datasets/{destination['datasetId']}/tables/{destination['tableId']}/data"
    ).json()
    assert body["totalRows"] == "1"
    assert body["rows"] == [{"f": [{"v": "1"}, {"v": "x"}]}]


def test_tabledata_list_of_a_view_is_unsupported(client):
    make_big_table(client)
    insert_query(client, "CREATE VIEW ds.v AS SELECT x FROM ds.big")
    response = list_data(client, table="v")
    assert response.status_code == 400
    error = response.json()["error"]["errors"][0]
    assert error["reason"] == "invalidQuery"
    assert error["message"].startswith("Not supported by Crucible")
    assert "tabledata.list of a view" in error["message"]


def test_tabledata_list_of_a_missing_table_is_a_404(client):
    make_dataset(client)
    response = list_data(client, table="missing")
    assert response.status_code == 404
    assert response.json()["error"]["errors"][0]["reason"] == "notFound"


def test_tabledata_list_of_a_missing_dataset_is_a_404(client):
    response = list_data(client, table="t", dataset="nods")
    assert response.status_code == 404


def test_tabledata_list_with_selected_fields_is_unsupported(client):
    make_big_table(client)
    response = list_data(client, selectedFields="x")
    assert response.status_code == 400
    assert response.json()["error"]["errors"][0]["reason"] == "invalidQuery"


def test_tabledata_list_with_an_empty_selected_fields_is_the_full_row(client):
    make_big_table(client)
    body = list_data(client, maxResults=1, selectedFields="").json()
    assert values(body["rows"]) == ["1"]


@pytest.mark.parametrize(
    "params",
    [
        {"formatOptions.timestampOutputFormat": "ISO8601_STRING"},
        {"fields": "rows"},
        {"unknownParam": "1"},
    ],
)
def test_tabledata_list_with_a_parameter_that_changes_the_answer_is_unsupported(client, params):
    make_big_table(client)
    response = list_data(client, **params)
    assert response.status_code == 400
    assert response.json()["error"]["errors"][0]["reason"] == "invalidQuery"


def test_tabledata_list_with_a_non_integer_max_results_is_invalid(client):
    make_big_table(client)
    response = list_data(client, maxResults="ten")
    assert response.status_code == 400
    assert response.json()["error"]["errors"][0]["reason"] == "invalidQuery"


def test_table_options_in_a_dataset_with_a_default_expiration_is_unsupported(client):
    # BigQuery lists each table's expiration_timestamp there; object times are not tracked.
    make_dataset(client, defaultTableExpirationMs="3600000")
    insert_query(client, "CREATE TABLE ds.t (a INT64)")
    response = run_query(client, "SELECT option_name FROM ds.INFORMATION_SCHEMA.TABLE_OPTIONS")
    assert response.status_code == 400
    assert "default table expiration" in response.json()["error"]["message"]


# --- copy jobs (copy.py) -------------------------------------------------------------------------
def copy_job(client: TestClient, source: str, destination: str, **extra: Any) -> Any:
    def ref(table: str) -> dict[str, str]:
        return {"projectId": PROJECT, "datasetId": "ds", "tableId": table}

    copy = {"sourceTables": [ref(source)], "destinationTable": ref(destination), **extra}
    return client.post(f"{API}/jobs", json={"configuration": {"copy": copy}}).json()


@pytest.fixture
def partitioned(client):
    make_dataset(client)
    insert_query(client, "CREATE TABLE ds.m (d DATE, k INT64) PARTITION BY d")
    insert_query(client, "INSERT INTO ds.m VALUES (DATE '2024-01-01', 1), (DATE '2024-01-02', 2)")
    insert_query(
        client,
        "CREATE TABLE ds.tmp PARTITION BY d AS "
        "SELECT DATE '2024-01-02' AS d, 20 AS k UNION ALL SELECT DATE '2024-01-03', 30",
    )
    return client


def test_a_partition_copy_with_write_truncate_replaces_only_that_partition(partitioned):
    job = copy_job(partitioned, "tmp$20240102", "m$20240102", writeDisposition="WRITE_TRUNCATE")
    assert job["status"] == {"state": "DONE"}
    assert job["configuration"]["jobType"] == "COPY"
    rows = run_query(partitioned, "SELECT CAST(d AS STRING), k FROM ds.m ORDER BY 1").json()
    assert values(rows["rows"]) == ["2024-01-01", "1", "2024-01-02", "20"]
    assert partitioned.get(f"{API}/jobs/{job['jobReference']['jobId']}").json()["status"] == {
        "state": "DONE"
    }


def test_a_table_copy_creates_the_destination_with_the_source_partitioning(partitioned):
    assert copy_job(partitioned, "tmp", "fresh")["status"] == {"state": "DONE"}
    table = partitioned.get(f"{API}/datasets/ds/tables/fresh").json()
    assert table["timePartitioning"] == {"type": "DAY", "field": "d"}
    assert table["numRows"] == "2"


def test_write_append_adds_and_write_empty_refuses_a_non_empty_table(partitioned):
    assert copy_job(partitioned, "tmp", "m", writeDisposition="WRITE_APPEND")["status"] == {
        "state": "DONE"
    }
    assert values(run_query(partitioned, "SELECT COUNT(*) FROM ds.m").json()["rows"]) == ["4"]
    refused = copy_job(partitioned, "tmp", "m")  # WRITE_EMPTY is the default
    assert refused["status"]["errorResult"]["message"].startswith("Not supported by Crucible")


@pytest.mark.parametrize(
    ("source", "destination"),
    [("tmp$20240102", "m$20240103"), ("tmp$2024010", "m$2024010"), ("tmp$__NULL__", "m$__NULL__")],
)
def test_decorators_crucible_cannot_match_are_refused(partitioned, source, destination):
    job = copy_job(partitioned, source, destination, writeDisposition="WRITE_TRUNCATE")
    assert job["status"]["errorResult"]["message"].startswith("Not supported by Crucible")


def test_routines_list_and_get_report_persistent_functions(client):
    make_dataset(client)
    insert_query(client, "CREATE FUNCTION ds.g(x FLOAT64) RETURNS FLOAT64 AS (x * 2)")
    listed = client.get(f"{API}/datasets/ds/routines").json()["routines"]
    assert [r["routineReference"]["routineId"] for r in listed] == ["g"]
    assert "definitionBody" not in listed[0]
    routine = client.get(f"{API}/datasets/ds/routines/g").json()
    assert routine["routineType"] == "SCALAR_FUNCTION"
    assert routine["language"] == "SQL"
    assert routine["arguments"] == [{"name": "x", "dataType": {"typeKind": "FLOAT64"}}]
    assert routine["returnType"] == {"typeKind": "FLOAT64"}
    assert routine["definitionBody"] == "x * 2"  # routines_get_function


# --- tables.insert with partitioning and clustering ------------------------------------------
PARTITIONED_SCHEMA = {
    "fields": [
        {"name": "d", "type": "DATE"},
        {"name": "ts", "type": "TIMESTAMP"},
        {"name": "n", "type": "INTEGER"},
        {"name": "s", "type": "STRING"},
        {"name": "f", "type": "FLOAT"},
    ]
}


def insert_table(client: TestClient, table: str, **body: Any) -> Any:
    reference = {"projectId": PROJECT, "datasetId": "ds", "tableId": table}
    return client.post(
        f"{API}/datasets/ds/tables",
        json={"tableReference": reference, "schema": PARTITIONED_SCHEMA, **body},
    )


@pytest.mark.parametrize(
    "partitioning",
    [
        {"timePartitioning": {"type": "DAY", "field": "d"}},
        {"timePartitioning": {"type": "MONTH", "field": "d"}},
        {"timePartitioning": {"type": "HOUR", "field": "ts"}},
        {
            "rangePartitioning": {
                "field": "n",
                "range": {"start": "0", "end": "100", "interval": "10"},
            }
        },
    ],
)
def test_tables_insert_partitioning_round_trips_through_tables_get(client, partitioning):
    make_dataset(client)
    clustering = {"clustering": {"fields": ["s", "n"]}}
    created = insert_table(client, "p", **partitioning, **clustering)
    assert created.status_code == 200, created.json()
    got = client.get(f"{API}/datasets/ds/tables/p").json()
    for key, value in {**partitioning, **clustering}.items():
        assert got[key] == value


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"timePartitioning": {"type": "DAY", "field": "s"}},
            "The field specified for time partitioning can only be of type TIMESTAMP, DATE or "
            "DATETIME. The type found is: STRING.",
        ),
        (
            {
                "rangePartitioning": {
                    "field": "s",
                    "range": {"start": "0", "end": "9", "interval": "1"},
                }
            },
            "The field specified for integer range partitioning can only be of type INT64. The "
            "type found is: STRING.",
        ),
        (
            {"timePartitioning": {"type": "DAY", "field": "nope"}},
            "The field specified for partitioning cannot be found in the schema.",
        ),
        (
            {"clustering": {"fields": ["f"]}},
            "Field f has type FLOAT, which is not supported for clustering.",
        ),
        (
            {"clustering": {"fields": ["d", "ts", "n", "s", "d"]}},
            "Field can only appear once in clustering fields. Invalid field: d.",
        ),
        (
            {"timePartitioning": {"type": "HOUR", "field": "d"}},
            "The field specified for hourly partitioning can only be of type TIMESTAMP or "
            "DATETIME. The type found is: DATE.",
        ),
    ],
)
def test_tables_insert_rejects_invalid_partitioning_and_clustering(client, body, message):
    make_dataset(client)
    response = insert_table(client, "bad", **body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == message
    assert error["errors"][0]["reason"] == "invalid"
    assert client.get(f"{API}/datasets/ds/tables/bad").status_code == 404


@pytest.mark.parametrize(
    ("body", "construct"),
    [
        ({"timePartitioning": {"type": "DAY"}}, "ingestion-time partitioning"),
        (
            {"timePartitioning": {"type": "DAY", "field": "d", "expirationMs": "1000"}},
            "timePartitioning field expirationMs",
        ),
        (
            {"requirePartitionFilter": True, "timePartitioning": {"field": "d"}},
            "requirePartitionFilter",
        ),
    ],
)
def test_tables_insert_refuses_partitioning_that_would_change_results(client, body, construct):
    make_dataset(client)
    response = insert_table(client, "refused", **body)
    assert response.json()["error"]["message"] == f"Not supported by Crucible: {construct}"


# --- query jobs with a destination table ----------------------------------------------------
def to_table(table: str, **extra: Any) -> dict[str, Any]:
    reference = {"projectId": PROJECT, "datasetId": "ds", "tableId": table}
    return {"destinationTable": reference, **extra}


def table_rows(client: TestClient, table: str) -> list[str]:
    return values(run_query(client, f"SELECT * FROM ds.{table} ORDER BY 1").json().get("rows", []))


def test_a_query_creates_its_destination_and_returns_its_rows(client):
    make_dataset(client)
    job = insert_query(client, "SELECT 1 AS k, 'a' AS v", configuration_extra=to_table("dest"))
    body = job.json()
    assert body["status"] == {"state": "DONE"}
    assert body["configuration"]["query"]["destinationTable"]["tableId"] == "dest"
    results = client.get(f"{API}/queries/{body['jobReference']['jobId']}").json()
    assert values(results["rows"]) == ["1", "a"]
    assert client.get(f"{API}/datasets/ds/tables/dest").json()["numRows"] == "1"


def test_write_truncate_replaces_and_write_append_adds(client):
    make_dataset(client)
    insert_query(client, "SELECT 1 AS k", configuration_extra=to_table("dest"))
    truncate = to_table("dest", writeDisposition="WRITE_TRUNCATE")
    insert_query(client, "SELECT 2 AS k", configuration_extra=truncate)
    assert table_rows(client, "dest") == ["2"]
    append = to_table("dest", writeDisposition="WRITE_APPEND")
    insert_query(client, "SELECT 3 AS k", configuration_extra=append)
    assert table_rows(client, "dest") == ["2", "3"]
    assert client.get(f"{API}/datasets/ds/tables/dest").json()["numRows"] == "2"


def test_write_empty_fails_on_a_table_with_rows(client):
    make_dataset(client)
    insert_query(client, "SELECT 1 AS k", configuration_extra=to_table("dest"))
    job = insert_query(client, "SELECT 2 AS k", configuration_extra=to_table("dest")).json()
    assert job["status"]["errorResult"]["message"].startswith("Already Exists: Table")
    assert table_rows(client, "dest") == ["1"]


def test_create_never_needs_an_existing_table(client):
    make_dataset(client)
    never = to_table("missing", createDisposition="CREATE_NEVER")
    job = insert_query(client, "SELECT 1 AS k", configuration_extra=never).json()
    assert job["status"]["errorResult"]["reason"] == "notFound"


def test_a_failed_query_leaves_the_destination_untouched(client):
    make_dataset(client)
    insert_query(client, "SELECT 1 AS k", configuration_extra=to_table("dest"))
    truncate = to_table("dest", writeDisposition="WRITE_TRUNCATE")
    job = insert_query(client, "SELECT 1 / 0 AS k", configuration_extra=truncate).json()
    assert "errorResult" in job["status"]
    assert table_rows(client, "dest") == ["1"]


def test_named_parameters_reach_a_destination_query(client):
    make_dataset(client)
    extra = to_table(
        "dest",
        parameterMode="NAMED",
        queryParameters=[
            {"name": "x", "parameterType": {"type": "INT64"}, "parameterValue": {"value": "7"}}
        ],
    )
    insert_query(client, "SELECT @x AS k", configuration_extra=extra)
    assert table_rows(client, "dest") == ["7"]


@pytest.mark.parametrize(
    ("sql", "extra", "construct"),
    [
        ("INSERT INTO ds.t VALUES (1)", {}, "a destination table for a INSERT statement"),
        ("SELECT 1 AS k; SELECT 2 AS k", {}, "a destination table for a multi-statement query"),
        (
            "SELECT 1 AS k",
            {"writeDisposition": "WRITE_APPEND", "k": 1},
            "query job configuration field k",
        ),
    ],
)
def test_destination_shapes_crucible_does_not_emulate_are_refused(client, sql, extra, construct):
    make_dataset(client)
    run_query(client, "CREATE TABLE ds.t (k INT64)")
    job = insert_query(client, sql, configuration_extra=to_table("dest", **extra)).json()
    message = job.get("status", {}).get("errorResult", {}).get("message") or job["error"]["message"]
    assert message == f"Not supported by Crucible: {construct}"


# --- Parquet load jobs (ADR 0015) -----------------------------------------------------------
PARQUET_FIELDS = [
    {"name": "k", "type": "INTEGER", "mode": "REQUIRED"},
    {"name": "s", "type": "STRING"},
]


def parquet_bytes(keys: list[int | None], strings: list[str | None]) -> bytes:
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    sink = io.BytesIO()
    pq.write_table(pa.table({"k": pa.array(keys, pa.int64()), "s": pa.array(strings)}), sink)
    return sink.getvalue()


def load_parquet(client: TestClient, table: str, data: bytes, **config: Any) -> dict[str, Any]:
    body = {
        "jobReference": {"projectId": PROJECT, "jobId": f"pq_{uuid.uuid4().hex[:8]}"},
        "configuration": {
            "load": {
                "destinationTable": {"projectId": PROJECT, "datasetId": "ds", "tableId": table},
                "schema": {"fields": PARQUET_FIELDS},
                "sourceFormat": "PARQUET",
                "writeDisposition": "WRITE_APPEND",
                **config,
            }
        },
    }
    url = upload_url(client.post(UPLOAD, params={"uploadType": "resumable"}, json=body))
    response = client.put(
        url, content=data, headers={"Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"}
    )
    assert response.status_code == 200
    job: dict[str, Any] = response.json()
    return job


def test_a_parquet_load_keeps_required_fields_and_counts_output_rows(client):
    make_dataset(client)
    job = load_parquet(client, "pq", parquet_bytes([1, 2], ["a", None]))
    assert job["status"] == {"state": "DONE"}
    assert job["statistics"]["load"]["outputRows"] == "2"
    fields = client.get(f"{API}/datasets/ds/tables/pq").json()["schema"]["fields"]
    assert [f.get("mode", "NULLABLE") for f in fields] == ["REQUIRED", "NULLABLE"]
    load_parquet(client, "pq", parquet_bytes([3], ["c"]))
    assert values(run_query(client, "SELECT k FROM ds.pq ORDER BY k").json()["rows"]) == [
        "1",
        "2",
        "3",
    ]
    load_parquet(client, "pq", parquet_bytes([9], ["z"]), writeDisposition="WRITE_TRUNCATE")
    assert values(run_query(client, "SELECT k, s FROM ds.pq").json()["rows"]) == ["9", "z"]
    fields = client.get(f"{API}/datasets/ds/tables/pq").json()["schema"]["fields"]
    assert fields[0]["mode"] == "REQUIRED"


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (
            b"not parquet",
            {
                "reason": "invalid",
                "message": "Error while reading data, error message: Input file is not in "
                "Parquet format.",
            },
        ),
        (
            parquet_bytes([1, None], ["a", "b"]),
            {
                "reason": "invalidQuery",
                "location": "query",
                "message": "Required field k cannot be null",
            },
        ),
    ],
    ids=["unreadable", "null-in-required"],
)
def test_a_failed_parquet_load_appends_nothing(client, data, error):
    make_dataset(client)
    load_parquet(client, "pq", parquet_bytes([1], ["a"]))
    for disposition in ("WRITE_APPEND", "WRITE_TRUNCATE"):
        job = load_parquet(client, "pq", data, writeDisposition=disposition)
        assert job["status"]["errorResult"] == error  # parquet_unreadable, parquet_required_null
        assert values(run_query(client, "SELECT k FROM ds.pq").json()["rows"]) == ["1"]


def test_a_multipart_parquet_upload_loads(client):
    make_dataset(client)
    metadata = load_metadata("mp_pq", schema={"fields": PARQUET_FIELDS}, sourceFormat="PARQUET")
    metadata = json.dumps(
        {
            **json.loads(metadata),
            "configuration": {
                "load": {
                    k: v
                    for k, v in json.loads(metadata)["configuration"]["load"].items()
                    if k not in ("fieldDelimiter", "skipLeadingRows")
                }
            },
        }
    ).encode()
    response = multipart(client, metadata, parquet_bytes([5, 6], ["e", "f"]))
    assert response.json()["status"] == {"state": "DONE"}
    assert values(run_query(client, "SELECT COUNT(*) FROM ds.mp_pq").json()["rows"]) == ["2"]
