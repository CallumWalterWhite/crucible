"""BigQuery REST server behaviour, one test per behaviour, with raw REST calls (no google client).

Field-by-field agreement with the recorded fixtures is tested by test_bigquery_fixtures.py; this
file tests what the endpoints do: paging, job lifecycle, errors, datasets, tables, uploads and
concurrency.
"""

from __future__ import annotations

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


def test_dry_run_in_jobs_insert_is_unsupported(client):
    response = client.post(
        f"{API}/jobs",
        json={
            "configuration": {"dryRun": True, "query": {"query": "SELECT 1", "useLegacySql": False}}
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


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


def test_a_multipart_upload_is_unsupported(client):
    response = client.post(f"{UPLOAD}", params={"uploadType": "multipart"}, json={})
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Not supported by Crucible")


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
