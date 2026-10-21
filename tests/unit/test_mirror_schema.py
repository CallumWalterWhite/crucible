"""`crucible mirror-schema`: schemas from a BigQuery project land in a live Crucible, and the Google
side is read-only. The Google client runs against a canned REST session; Crucible runs in-process on
a loopback socket, as tests/corpus/test_corpus_wire.py does."""

# ruff: noqa: E402 - the client imports follow the importorskip guards

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest

bigquery = pytest.importorskip("google.cloud.bigquery")
uvicorn = pytest.importorskip("uvicorn")
requests = pytest.importorskip("requests")

from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import NotFound
from google.auth.credentials import AnonymousCredentials

from crucible.cli import main
from crucible.engine import Engine
from crucible.mirror import mirror_schema
from crucible.server.app import create_app

SOURCE, TARGET = "src", "dst"
DATASETS = ["raw", "marts"]


def _table_ref(dataset: str, table: str) -> dict[str, str]:
    return {"projectId": SOURCE, "datasetId": dataset, "tableId": table}


def _resources() -> dict[str, Any]:
    """Canned datasets.get, tables.list and tables.get responses, keyed by REST path."""
    events = {
        "tableReference": _table_ref("raw", "events"),
        "type": "TABLE",
        "description": 'Click "events", one row per click',
        "labels": {"owner": "growth"},
        "schema": {
            "fields": [
                {"name": "id", "type": "INTEGER", "mode": "REQUIRED", "description": "Event id"},
                {"name": "ts", "type": "TIMESTAMP", "mode": "NULLABLE"},
                {"name": "user_id", "type": "STRING", "mode": "NULLABLE"},
                {
                    "name": "items",
                    "type": "RECORD",
                    "mode": "REPEATED",
                    "fields": [{"name": "sku", "type": "STRING", "mode": "NULLABLE"}],
                },
            ]
        },
        "timePartitioning": {"type": "DAY", "field": "ts"},
        "clustering": {"fields": ["user_id"]},
    }
    ranged = {
        "tableReference": _table_ref("raw", "ranged"),
        "type": "TABLE",
        "schema": {"fields": [{"name": "n", "type": "INTEGER", "mode": "NULLABLE"}]},
        "rangePartitioning": {"field": "n", "range": {"start": 0, "end": 100, "interval": 10}},
    }
    geo = {
        "tableReference": _table_ref("raw", "geo"),
        "type": "TABLE",
        "schema": {"fields": [{"name": "g", "type": "GEOGRAPHY", "mode": "NULLABLE"}]},
    }
    constant = {
        "tableReference": _table_ref("marts", "constant"),
        "type": "VIEW",
        "view": {"query": "SELECT 1 AS one", "useLegacySql": False},
        "schema": {"fields": [{"name": "one", "type": "INTEGER", "mode": "NULLABLE"}]},
    }
    daily = {
        "tableReference": _table_ref("marts", "daily"),
        "type": "VIEW",
        "view": {"query": f"SELECT id FROM `{SOURCE}.raw.events`", "useLegacySql": False},
        "schema": {"fields": [{"name": "id", "type": "INTEGER", "mode": "REQUIRED"}]},
    }
    marts_listing = {
        "tables": [
            {"tableReference": t["tableReference"], "type": t["type"]} for t in (constant, daily)
        ]
    }
    raw_listing = {
        "tables": [
            {"tableReference": t["tableReference"], "type": t["type"]}
            for t in (events, ranged, geo)
        ]
    }
    base = f"projects/{SOURCE}/datasets"
    return {
        f"{base}/raw": {
            "datasetReference": {"projectId": SOURCE, "datasetId": "raw"},
            "description": "Raw events",
            "labels": {"team": "data"},
        },
        f"{base}/marts": {"datasetReference": {"projectId": SOURCE, "datasetId": "marts"}},
        f"{base}/raw/tables": raw_listing,
        f"{base}/marts/tables": marts_listing,
        f"{base}/raw/tables/events": events,
        f"{base}/raw/tables/ranged": ranged,
        f"{base}/raw/tables/geo": geo,
        f"{base}/marts/tables/constant": constant,
        f"{base}/marts/tables/daily": daily,
    }


class ReadOnlySession:
    """Serves the canned Google REST resources. Any verb but GET fails the test where it happens."""

    is_mtls = False  # read by google-cloud-bigquery to pick the endpoint

    def __init__(self, resources: dict[str, Any]) -> None:
        self.resources = resources
        self.paths: list[str] = []

    def request(self, method: str, url: str, **_kwargs: Any) -> requests.Response:
        assert method == "GET", f"{method} {url} reached Google"
        path = urlsplit(url).path.removeprefix("/bigquery/v2/")
        self.paths.append(path)
        body = self.resources.get(path)
        response = requests.Response()
        response.status_code = 200 if body is not None else 404
        payload = body if body is not None else {"error": {"code": 404, "message": path}}
        response._content = json.dumps(payload).encode()  # requests exposes no setter
        response.headers["Content-Type"] = "application/json"
        return response


@pytest.fixture
def crucible_url() -> Iterator[str]:
    engine = Engine()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(create_app("bigquery", engine), log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("the test server did not start")
        threading.Event().wait(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=10)
    engine.close()


def _clients(url: str) -> tuple[bigquery.Client, bigquery.Client, ReadOnlySession]:
    session = ReadOnlySession(_resources())
    source = bigquery.Client(
        project=SOURCE,
        credentials=AnonymousCredentials(),
        _http=session,  # pyright: ignore
    )
    target = bigquery.Client(
        project=TARGET,
        credentials=AnonymousCredentials(),  # pyright: ignore[reportArgumentType]
        client_options=ClientOptions(api_endpoint=url),
    )
    return source, target, session


def test_mirrors_table_and_view_schemas_into_crucible(crucible_url: str) -> None:
    source, target, _session = _clients(crucible_url)
    warnings: list[str] = []

    summary = mirror_schema(source, target, SOURCE, TARGET, DATASETS, warnings.append)

    assert (summary.datasets, summary.tables, summary.views, summary.skipped) == (2, 2, 1, 2)
    raw = target.get_dataset(f"{TARGET}.raw")
    assert (raw.description, raw.labels) == ("Raw events", {"team": "data"})

    events = target.get_table(f"{TARGET}.raw.events")
    assert events.description == 'Click "events", one row per click'
    assert events.labels == {"owner": "growth"}
    assert events.time_partitioning.field == "ts"
    assert events.time_partitioning.type_ == "DAY"
    assert events.clustering_fields == ["user_id"]
    assert [(f.name, f.field_type, f.mode, f.description) for f in events.schema] == [
        ("id", "INTEGER", "REQUIRED", "Event id"),
        ("ts", "TIMESTAMP", "NULLABLE", None),
        ("user_id", "STRING", "NULLABLE", None),
        ("items", "RECORD", "REPEATED", None),
    ]
    assert [(f.name, f.field_type) for f in events.schema[3].fields] == [("sku", "STRING")]

    ranged = target.get_table(f"{TARGET}.raw.ranged")
    assert (ranged.range_partitioning.field, ranged.range_partitioning.range_.end) == ("n", 100)

    view = target.get_table(f"{TARGET}.marts.constant")
    assert [f.name for f in view.schema] == ["one"]
    assert view.table_type == "VIEW"

    # Schemas only: nothing was inserted.
    assert target.get_table(f"{TARGET}.raw.events").num_rows == 0


def test_google_side_only_reads(crucible_url: str) -> None:
    source, target, session = _clients(crucible_url)

    mirror_schema(source, target, SOURCE, TARGET, DATASETS, lambda _message: None)

    assert session.paths, "the source was never read"
    assert all("/jobs" not in path and "/queries" not in path for path in session.paths)


def test_skips_warn_and_name_the_object(crucible_url: str) -> None:
    source, target, _session = _clients(crucible_url)
    warnings: list[str] = []

    mirror_schema(source, target, SOURCE, TARGET, DATASETS, warnings.append)

    assert len(warnings) == 2
    assert any("table raw.geo" in w and "GEOGRAPHY" in w for w in warnings)
    assert any("view marts.daily" in w for w in warnings)
    with pytest.raises(NotFound):
        target.get_table(f"{TARGET}.raw.geo")
    with pytest.raises(NotFound):
        target.get_table(f"{TARGET}.marts.daily")


def test_refuses_without_from_project_or_dataset() -> None:
    with pytest.raises(SystemExit) as no_project:
        main(["mirror-schema", "--dataset", "raw"])
    with pytest.raises(SystemExit) as no_dataset:
        main(["mirror-schema", "--from-project", SOURCE])
    assert no_project.value.code == 2
    assert no_dataset.value.code == 2


def test_cli_prints_summary_and_exits_zero(
    crucible_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source, target, _session = _clients(crucible_url)
    # The CLI builds its own clients; the source one is the canned session, the target one is live.
    monkeypatch.setattr(
        bigquery, "Client", lambda **kwargs: target if "client_options" in kwargs else source
    )

    argv = ["mirror-schema", "--from-project", SOURCE, "--to-project", TARGET, "--to", crucible_url]
    code = main([*argv, "--dataset", "raw", "--dataset", "marts"])

    out, err = capsys.readouterr()
    assert code == 0
    assert out == "mirrored 2 datasets, 2 tables, 1 views; skipped 2\n"
    assert err.count("warning: ") == 2
