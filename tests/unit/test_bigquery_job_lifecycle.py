"""Job publication, upload ownership and reset races, with explicit scheduling barriers."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.testclient import TestClient

from crucible.engine import Engine
from crucible.server.app import create_app
from crucible.server.bigquery.jobs import JobStore, Runner
from tests.unit.test_bigquery_server import (
    API,
    CSV,
    UPLOAD,
    initiate,
    insert_query,
    make_dataset,
    run_query,
    upload_url,
    values,
)


@pytest.fixture
def client():
    engine = Engine()
    with TestClient(create_app("bigquery", engine), raise_server_exceptions=False) as client:
        yield client
    engine.close()


@pytest.fixture
def completion_barrier(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JobStore.finished

    def finish(store, *jobs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "job completion was not released"
        return original(store, *jobs)

    monkeypatch.setattr(JobStore, "finished", finish)
    yield entered, release
    release.set()


def test_job_results_are_published_with_the_destination_table(client, completion_barrier):
    entered, release = completion_barrier
    with ThreadPoolExecutor(max_workers=1) as pool:
        submitted = pool.submit(insert_query, client, "SELECT 42 AS answer", job_id="running")
        try:
            assert entered.wait(10)
            job = client.get(f"{API}/jobs/running").json()
            assert job["status"] == {"state": "RUNNING"}
            assert "destinationTable" not in job["configuration"]["query"]
            results = client.get(f"{API}/queries/running").json()
            assert results["jobComplete"] is False
            assert not {"schema", "rows", "totalRows"} & results.keys()
        finally:
            release.set()
        job = submitted.result(timeout=10).json()

    assert job["status"] == {"state": "DONE"}
    target = job["configuration"]["query"]["destinationTable"]
    table = client.get(f"{API}/datasets/{target['datasetId']}/tables/{target['tableId']}")
    assert table.status_code == 200
    assert table.json()["numRows"] == "1"
    results = client.get(f"{API}/queries/running").json()
    assert results["jobComplete"] is True
    assert values(results["rows"]) == ["42"]


def test_load_statistics_are_published_only_when_finished(client, completion_barrier):
    make_dataset(client)
    initiated = client.post(
        UPLOAD,
        params={"uploadType": "resumable"},
        json={
            "jobReference": {"jobId": "loading"},
            "configuration": {
                "load": {
                    "destinationTable": {"datasetId": "ds", "tableId": "loaded"},
                    "schema": {"fields": [{"name": "x", "type": "INTEGER"}]},
                }
            },
        },
    )
    url = upload_url(initiated)
    entered, release = completion_barrier
    with ThreadPoolExecutor(max_workers=1) as pool:
        submitted = pool.submit(
            client.put,
            url,
            content=b"42\n",
            headers={"Content-Range": "bytes 0-2/3"},
        )
        try:
            assert entered.wait(10)
            job = client.get(f"{API}/jobs/loading").json()
            assert job["status"] == {"state": "RUNNING"}
            assert "load" not in job["statistics"]
        finally:
            release.set()
        completed = submitted.result(timeout=10).json()
    assert completed["status"] == {"state": "DONE"}
    assert completed["statistics"]["load"]["outputRows"] == "1"


@pytest.mark.parametrize("reuse_id", [False, True])
def test_reset_prevents_old_job_publication(client, completion_barrier, reuse_id):
    entered, release = completion_barrier
    with ThreadPoolExecutor(max_workers=1) as pool:
        submitted = pool.submit(insert_query, client, "SELECT 1 AS x", job_id="old")
        try:
            assert entered.wait(10)
            assert client.post("/_crucible/reset").status_code == 200
            if reuse_id:
                assert insert_query(client, "SELECT 2 AS x", job_id="old").status_code == 200
        finally:
            release.set()
        old = submitted.result(timeout=10).json()

    target = old["configuration"]["query"]["destinationTable"]
    assert (
        client.get(f"{API}/datasets/{target['datasetId']}/tables/{target['tableId']}").status_code
        == 404
    )
    if reuse_id:
        assert values(client.get(f"{API}/queries/old").json()["rows"]) == ["2"]
    else:
        assert client.get(f"{API}/jobs/old").status_code == 404


def test_concurrent_upload_finalizers_load_once_and_retries_return_the_job(client, monkeypatch):
    make_dataset(client)
    url = upload_url(initiate(client))
    buffered = client.put(url, content=CSV, headers={"Content-Range": f"bytes 0-{len(CSV) - 1}/*"})
    assert buffered.status_code == 308
    ready = asyncio.Event()
    arrived = 0
    original_body, original_load = Request.body, Runner._load
    loads = []

    async def body(request):
        nonlocal arrived
        chunk = await original_body(request)
        if request.method == "PUT":
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
        return chunk

    def load(runner, job, config, data):
        loads.append(data)
        return original_load(runner, job, config, data)

    monkeypatch.setattr(Request, "body", body)
    monkeypatch.setattr(Runner, "_load", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            pool.submit(
                client.put, url, content=b"", headers={"Content-Range": f"bytes */{len(CSV)}"}
            )
            for _ in range(2)
        ]
        responses = [request.result(timeout=10) for request in requests]
    assert [response.status_code for response in responses] == [200, 200]
    job = responses[0].json()
    assert responses[1].json()["jobReference"] == job["jobReference"]
    assert job["status"] == {"state": "DONE"}
    assert loads == [CSV]
    replay = client.put(
        url, content=CSV, headers={"Content-Range": f"bytes 0-{len(CSV) - 1}/{len(CSV)}"}
    )
    assert replay.status_code == 200
    assert replay.json()["jobReference"] == job["jobReference"]
    assert values(run_query(client, "SELECT COUNT(*) FROM ds.loaded").json()["rows"]) == ["3"]
    assert loads == [CSV]


def test_reset_invalidates_uploads_waiting_for_the_request_body(client, monkeypatch):
    make_dataset(client)
    url = upload_url(initiate(client))
    entered, release = threading.Event(), threading.Event()
    original = Request.body

    async def body(request):
        chunk = await original(request)
        if request.method == "PUT":
            entered.set()
            assert await run_in_threadpool(release.wait, 10)
        return chunk

    monkeypatch.setattr(Request, "body", body)
    with ThreadPoolExecutor(max_workers=1) as pool:
        uploaded = pool.submit(
            client.put,
            url,
            content=CSV,
            headers={"Content-Range": f"bytes 0-{len(CSV) - 1}/{len(CSV)}"},
        )
        try:
            assert entered.wait(10)
            assert client.post("/_crucible/reset").status_code == 200
        finally:
            release.set()
        assert uploaded.result(timeout=10).status_code == 404


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1,2 UNION ALL SELECT 3",
        "SELECT 1 UNION ALL SELECT 2,3",
        "SELECT 1,2 INTERSECT DISTINCT SELECT 3",
        "SELECT 1,2 EXCEPT DISTINCT SELECT 3",
        "SELECT 1,2 UNION ALL SELECT 3,4 UNION ALL SELECT 5",
    ],
)
def test_mismatched_set_operation_is_invalid_query(client, sql):
    response = run_query(client, sql)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["errors"][0]["reason"] == "invalidQuery"
    assert "mismatched column counts" in error["message"]
    job = insert_query(client, sql).json()
    assert job["status"]["state"] == "DONE"
    assert job["status"]["errorResult"]["reason"] == "invalidQuery"
