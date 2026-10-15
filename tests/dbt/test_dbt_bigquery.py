"""dbt-bigquery end to end against a running Crucible (CRUCIBLE_URL).

Marked `dbt`; CI runs `pytest -m dbt tests/dbt -k bigquery` against dbt-bigquery 1.9.0 and the
latest. The project in bigquery_smoke/ is invented: a seed, a table, a view, an incremental merge
model, generic tests and one unit test. Every run uses the dummy token and the endpoint from
BIGQUERY_EMULATOR_HOST (read by google-cloud-bigquery in both adapter versions), and writes its
target/ and logs/ to a temporary directory.
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.request
from collections.abc import Iterator
from unittest import mock

import pytest
from dbt.cli.main import dbtRunner
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

CRUCIBLE_URL = os.environ.get("CRUCIBLE_URL")
if not CRUCIBLE_URL:
    pytest.skip("CRUCIBLE_URL is not set", allow_module_level=True)

pytestmark = pytest.mark.dbt
PROJECT_DIR = pathlib.Path(__file__).resolve().parent / "bigquery_smoke"
PROJECT = "crucible-test"
DATASET = "smoke"
OK_STATUSES = {"success", "pass"}


@pytest.fixture(scope="module")
def dbt_workdir(tmp_path_factory) -> Iterator[pathlib.Path]:
    """Point dbt at the server, with the server reset first so the run starts empty."""
    request = urllib.request.Request(f"{CRUCIBLE_URL}/_crucible/reset", data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
    workdir = tmp_path_factory.mktemp("dbt")
    env = {"BIGQUERY_EMULATOR_HOST": CRUCIBLE_URL, "DBT_SEND_ANONYMOUS_USAGE_STATS": "False"}
    with mock.patch.dict(os.environ, env):
        yield workdir


def dbt(workdir: pathlib.Path, *args: str):
    """Run one dbt command in-process and return the dbtRunnerResult."""
    argv = [
        *args,
        "--project-dir",
        str(PROJECT_DIR),
        "--profiles-dir",
        str(PROJECT_DIR),
        "--target-path",
        str(workdir / "target"),
        "--log-path",
        str(workdir / "logs"),
    ]
    return dbtRunner().invoke(argv)


def assert_all_ok(result, command: str) -> None:
    assert result.success, f"dbt {command} failed: {result.exception!r}"
    statuses = {node.node.unique_id: node.status for node in result.result.results}
    bad = {uid: status for uid, status in statuses.items() if str(status) not in OK_STATUSES}
    assert not bad, f"dbt {command}: {bad}"


def count_rows(relation: str) -> int:
    client = bigquery.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": CRUCIBLE_URL},
    )
    rows = client.query(f"SELECT COUNT(*) AS n FROM `{PROJECT}.{DATASET}.{relation}`").result()
    return next(iter(rows)).n


def test_bigquery_seed_run_incremental_run_and_test(dbt_workdir):
    assert_all_ok(dbt(dbt_workdir, "seed"), "seed")
    assert_all_ok(dbt(dbt_workdir, "run"), "run")
    # The incremental model merges on customer_id, so a second pass must not duplicate rows.
    assert_all_ok(dbt(dbt_workdir, "run"), "run (incremental)")
    assert_all_ok(dbt(dbt_workdir, "test"), "test")

    assert count_rows("customers") == 6
    assert count_rows("vip_customers") == 3
    assert count_rows("customer_signups") == 6
    summary = json.loads((dbt_workdir / "target" / "run_results.json").read_text(encoding="utf-8"))
    assert summary["results"], "run_results.json lists no results"
