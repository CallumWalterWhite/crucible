"""Recording against a fake BigQuery client (no network), and the `corpus record` CLI guards."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from google.api_core.exceptions import BadRequest
from google.cloud.bigquery import Dataset, QueryJobConfig, SchemaField

from crucible.corpus import cli as corpus_cli
from crucible.corpus.case import Case, Expected, load_case
from crucible.corpus.record import BigQueryRecorder, RecordError, record_cases, standard_type
from tests.fakes import PassthroughDialect

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
LIMIT = 104857600


class FakeRows:
    def __init__(self, rows: Iterable[tuple[Any, ...]], schema: Iterable[SchemaField]) -> None:
        self._rows = list(rows)
        self.schema = list(schema)

    def __iter__(self) -> Iterator[FakeRow]:
        return iter(FakeRow(cells) for cells in self._rows)


class FakeRow:
    def __init__(self, cells: tuple[Any, ...]) -> None:
        self._cells = cells

    def values(self) -> tuple[Any, ...]:
        return self._cells


class FakeJob:
    def __init__(
        self,
        rows: Iterable[tuple[Any, ...]] = (),
        schema: Iterable[SchemaField] = (),
        error: Exception | None = None,
    ) -> None:
        self._rows, self._schema, self._error = list(rows), list(schema), error

    def result(self) -> FakeRows:
        if self._error is not None:
            raise self._error
        return FakeRows(self._rows, self._schema)


class FakeClient:
    """Records every dataset and job call. `answers` maps a SQL text to the job it returns."""

    project = "local-project"

    def __init__(self, answers: dict[str, FakeJob] | None = None) -> None:
        self.answers = answers or {}
        self.created: list[str] = []
        self.created_specs: list[Dataset] = []
        self.deleted: list[tuple[str, bool, bool]] = []
        self.jobs: list[tuple[str, QueryJobConfig]] = []

    def create_dataset(self, dataset: Dataset, exists_ok: bool = False, **_: Any) -> None:
        self.created.append(f"{dataset.project}.{dataset.dataset_id}")
        self.created_specs.append(dataset)

    def delete_dataset(
        self, dataset: str, delete_contents: bool = False, not_found_ok: bool = False, **_: Any
    ) -> None:
        self.deleted.append((dataset, delete_contents, not_found_ok))

    def query(self, sql: str, job_config: QueryJobConfig | None = None, **_: Any) -> FakeJob:
        assert job_config is not None
        self.jobs.append((sql, job_config))
        return self.answers.get(sql, FakeJob())


def make_case(
    root: Path, *, setup: list[str] | None = None, query: str = "SELECT 1 AS a", name: str = "case"
) -> Case:
    path = root / "corpus" / "passthrough" / "record" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "description": "a recorded case",
        "setup": setup or [],
        "query": query,
        "expected": {"columns": [{"name": "a", "type": "INT64"}], "rows": [[0]]},
        "verified": False,
        "recorded_at": None,
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return load_case(path)


def make_error_case(root: Path, name: str, query: str, kind: str, contains: str) -> Case:
    """A case that expects an error of `kind` whose message contains `contains`."""
    path = root / "corpus" / "passthrough" / "record" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "description": "an error case",
        "query": query,
        "expected": {"error": {"kind": kind, "message_contains": contains}},
        "verified": False,
        "recorded_at": None,
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return load_case(path)


def dataset_of(config: QueryJobConfig) -> str:
    ref = config.default_dataset
    assert ref is not None
    return f"{ref.project}.{ref.dataset_id}"


def recorder_for(client: FakeClient) -> BigQueryRecorder:
    return BigQueryRecorder(client, PassthroughDialect(), LIMIT)  # type: ignore[arg-type]


def test_dataset_is_created_then_deleted_and_every_job_is_capped(tmp_path: Path):
    client = FakeClient({"SELECT 1 AS a": FakeJob([(1,)], [SchemaField("a", "INTEGER")])})
    case = make_case(tmp_path, setup=["CREATE TABLE t (a INT64)"])

    expected = recorder_for(client).record(case)

    assert expected.columns == [("a", "INT64")]
    assert expected.rows == [[1]]
    [dataset] = client.created
    assert dataset.startswith("local-project.crucible_corpus_")
    assert len(dataset.split("_")[-1]) == 12
    [spec] = client.created_specs
    assert spec.default_table_expiration_ms == 3_600_000
    assert spec.labels == {"crucible-corpus": "1"}
    assert client.deleted == [(dataset, True, True)]
    assert [sql for sql, _ in client.jobs] == ["CREATE TABLE t (a INT64)", "SELECT 1 AS a"]
    for _, config in client.jobs:
        assert config.maximum_bytes_billed == LIMIT
        assert config.use_legacy_sql is False
        assert config.use_query_cache is False
        assert dataset_of(config) == dataset


def test_dataset_is_deleted_when_the_query_raises(tmp_path: Path):
    client = FakeClient(
        {"SELECT 1 AS a": FakeJob(error=BadRequest("Syntax error: unexpected token"))}
    )
    case = make_case(tmp_path)

    expected = recorder_for(client).record(case)

    assert expected == Expected(error_message="Syntax error: unexpected token")
    [(dataset, delete_contents, not_found_ok)] = client.deleted
    assert (dataset, delete_contents, not_found_ok) == (client.created[0], True, True)


def test_dataset_is_deleted_when_setup_fails_and_nothing_is_recorded(tmp_path: Path):
    client = FakeClient(
        {"CREATE TABLE t (a INT64)": FakeJob(error=BadRequest("Already exists: t"))}
    )
    case = make_case(tmp_path, setup=["CREATE TABLE t (a INT64)"])

    with pytest.raises(RecordError, match="setup failed: Already exists: t"):
        recorder_for(client).record(case)
    assert [d for d, _, _ in client.deleted] == client.created


def test_hitting_the_bytes_cap_is_not_recorded_as_an_answer(tmp_path: Path):
    message = "Query exceeded limit for bytes billed: 104857600. 2000000000 or higher required."
    client = FakeClient({"SELECT 1 AS a": FakeJob(error=BadRequest(message))})
    case = make_case(tmp_path)

    with pytest.raises(RecordError, match="maximum_bytes_billed"):
        recorder_for(client).record(case)
    assert [d for d, _, _ in client.deleted] == client.created


def test_repeated_and_record_columns_get_standard_names():
    schema = [
        SchemaField("tags", "STRING", mode="REPEATED"),
        SchemaField(
            "s",
            "RECORD",
            fields=[SchemaField("a", "INTEGER"), SchemaField("b", "STRING", mode="REPEATED")],
        ),
        SchemaField("n", "FLOAT"),
        SchemaField("ok", "BOOLEAN"),
        SchemaField("pts", "RECORD", mode="REPEATED", fields=[SchemaField("x", "NUMERIC")]),
    ]
    assert [standard_type(f) for f in schema] == [
        "ARRAY<STRING>",
        "STRUCT<a INT64, b ARRAY<STRING>>",
        "FLOAT64",
        "BOOL",
        "ARRAY<STRUCT<x NUMERIC>>",
    ]


def test_record_result_uses_standard_type_names(tmp_path: Path):
    schema = [SchemaField("tags", "STRING", mode="REPEATED"), SchemaField("n", "INTEGER")]
    client = FakeClient({"SELECT 1 AS a": FakeJob([(["x"], 1)], schema)})

    expected = recorder_for(client).record(make_case(tmp_path))

    assert expected.columns == [("tags", "ARRAY<STRING>"), ("n", "INT64")]
    assert expected.rows == [[["x"], 1]]


def test_unknown_bigquery_type_stops_recording(tmp_path: Path):
    client = FakeClient({"SELECT 1 AS a": FakeJob([(1,)], [SchemaField("r", "RANGE")])})
    with pytest.raises(RecordError, match="no Standard SQL name for BigQuery type RANGE"):
        recorder_for(client).record(make_case(tmp_path))


def test_record_cases_writes_only_the_three_fields(tmp_path: Path):
    client = FakeClient({"SELECT 1 AS a": FakeJob([(1,)], [SchemaField("a", "INTEGER")])})
    case = make_case(tmp_path)
    before = yaml.safe_load(case.path.read_text(encoding="utf-8"))

    record_cases([case], recorder_for(client), now=lambda: NOW)

    after = yaml.safe_load(case.path.read_text(encoding="utf-8"))
    assert list(after) == list(before)
    assert after["expected"] == {"columns": [{"name": "a", "type": "INT64"}], "rows": [[1]]}
    assert after["verified"] is True
    assert after["recorded_at"] == "2026-10-14T12:00:00Z"
    for key in ("description", "setup", "query"):
        assert after[key] == before[key]


def test_record_cases_keeps_a_hand_written_kind_and_message_contains(tmp_path: Path):
    client = FakeClient({"SELECT 1 AS a": FakeJob(error=BadRequest("Division by zero: 1 / 0"))})
    case = make_error_case(tmp_path, "case", "SELECT 1 AS a", "runtime", "Division by zero")

    mismatched = record_cases([case], recorder_for(client), now=lambda: NOW)

    assert mismatched == []
    reloaded = load_case(case.path)
    assert reloaded.expected == Expected(
        error_message="Division by zero: 1 / 0",
        error_contains="Division by zero",
        error_kind="runtime",
    )
    assert reloaded.verified is True


def test_mismatched_case_is_written_verbatim_and_later_cases_still_record(tmp_path: Path):
    client = FakeClient(
        {
            "SELECT 1 AS a": FakeJob(error=BadRequest("Unrelated failure")),
            "SELECT 2 AS a": FakeJob([(2,)], [SchemaField("a", "INTEGER")]),
        }
    )
    mismatched = make_error_case(
        tmp_path, "a-mismatch", "SELECT 1 AS a", "runtime", "Division by zero"
    )
    later = make_case(tmp_path, name="b-later", query="SELECT 2 AS a")
    before = yaml.safe_load(mismatched.path.read_text(encoding="utf-8"))

    returned = record_cases([mismatched, later], recorder_for(client), now=lambda: NOW)

    # The function reports exactly the mismatched case and keeps going past it.
    assert returned == [mismatched]
    after = yaml.safe_load(mismatched.path.read_text(encoding="utf-8"))
    assert list(after) == list(before)
    assert after["description"] == before["description"]
    assert after["query"] == before["query"]
    assert after["expected"] == {
        "error": {
            "kind": "runtime",
            "message": "Unrelated failure",
            "message_contains": "Division by zero",
        }
    }
    assert after["verified"] is False
    assert after["recorded_at"] == "2026-10-14T12:00:00Z"

    later_loaded = load_case(later.path)
    assert later_loaded.verified is True
    assert later_loaded.expected == Expected(columns=[("a", "INT64")], rows=[[2]])


class ForbiddenClient:
    """Stands in for google.cloud.bigquery.Client: constructing it fails the test."""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise AssertionError("a BigQuery client was constructed")


def record_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "corpus_command": "record",
        "dialect": "bigquery",
        "path": None,
        "max_bytes_billed": LIMIT,
        "confirmed": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_record_without_the_confirmation_flag_exits_2_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from google.cloud import bigquery

    monkeypatch.setattr(bigquery, "Client", ForbiddenClient)
    monkeypatch.setenv("BIGQUERY_PROJECT", "local-project")

    assert corpus_cli.main(record_args(confirmed=False)) == 2
    assert "refusing to record" in capsys.readouterr().err


def test_record_without_a_project_exits_2_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from google.cloud import bigquery

    monkeypatch.setattr(bigquery, "Client", ForbiddenClient)
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)

    assert corpus_cli.main(record_args(confirmed=True)) == 2
    assert "BIGQUERY_PROJECT" in capsys.readouterr().err


def test_snowflake_recording_exits_2(capsys: pytest.CaptureFixture[str]):
    assert corpus_cli.main(record_args(dialect="snowflake", confirmed=True)) == 2
    assert "not available until the Snowflake dialect exists" in capsys.readouterr().err


def test_record_announces_the_cap_before_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    make_case(tmp_path)
    monkeypatch.setattr(corpus_cli, "get_dialect", lambda _name: PassthroughDialect())
    monkeypatch.setenv("BIGQUERY_PROJECT", "local-project")
    from google.cloud import bigquery

    monkeypatch.setattr(bigquery, "Client", lambda project: FakeClient())

    assert corpus_cli.main(record_args(confirmed=True, path=str(tmp_path / "corpus"))) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    jobs = int(first.split("(")[1].split()[0])
    assert first == (
        f"Recording 1 cases ({jobs} jobs) against project local-project with "
        f"maximum_bytes_billed={LIMIT} per job; worst case {jobs * LIMIT} bytes billed in total"
    )


def test_run_prints_failures_and_a_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    good = make_case(tmp_path)
    monkeypatch.setattr(corpus_cli, "get_dialect", lambda _name: PassthroughDialect())

    # The case declares INT64 for "SELECT 1 AS a"; DuckDB reports INT, so the columns differ.
    code = corpus_cli.main(
        argparse.Namespace(corpus_command="run", dialect="bigquery", path=str(tmp_path / "corpus"))
    )
    out = capsys.readouterr().out
    assert code == 1
    assert out.splitlines()[0].startswith(f"FAIL {good.path}: columns differ")
    assert out.splitlines()[-1] == "0 passed, 1 failed (0 verified, 1 unverified)"


def test_run_for_a_dialect_not_built_yet_exits_2(capsys: pytest.CaptureFixture[str]):
    code = corpus_cli.main(argparse.Namespace(corpus_command="run", dialect="snowflake", path=None))
    assert code == 2
    assert "not built yet" in capsys.readouterr().err


def test_clean_message_keeps_bigquery_message_and_hides_project():
    from crucible.corpus.record import clean_message

    raw = (
        "Not found: Table my-sandbox:crucible_corpus_0123456789ab.t was not found in location US; "
        "reason: notFound, message: Not found: Table my-sandbox:crucible_corpus_0123456789ab.t was "
        "not found in location US\n\nLocation: US\nJob ID: 00000000-0000-0000-0000-000000000000\n"
    )
    assert clean_message(raw, "my-sandbox", "crucible_corpus_0123456789ab") == (
        "Not found: Table <project>:<dataset>.t was not found in location US"
    )
    assert clean_message("division by zero: 1 / 0", "p", "d") == "division by zero: 1 / 0"


def test_clean_message_drops_the_request_prefix():
    from crucible.corpus.record import clean_message

    raw = (
        "GET https://bigquery.googleapis.com/bigquery/v2/projects/p/queries/abc?maxResults=0: "
        "UPDATE/MERGE must match at most one source row for each target row"
    )
    assert clean_message(raw, "p", "d") == (
        "UPDATE/MERGE must match at most one source row for each target row"
    )


def test_record_unverified_only_skips_verified_cases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    make_case(tmp_path, name="open")
    done = make_case(tmp_path, name="done")
    done.path.write_text(
        done.path.read_text(encoding="utf-8")
        .replace("verified: false", "verified: true")
        .replace("recorded_at: null", "recorded_at: '2026-01-01T00:00:00Z'"),
        encoding="utf-8",
    )
    monkeypatch.setattr(corpus_cli, "get_dialect", lambda _name: PassthroughDialect())
    monkeypatch.setenv("BIGQUERY_PROJECT", "local-project")
    from google.cloud import bigquery

    monkeypatch.setattr(bigquery, "Client", lambda project: FakeClient())

    args = record_args(confirmed=True, path=str(tmp_path / "corpus"), unverified_only=True)
    assert corpus_cli.main(args) == 0
    assert capsys.readouterr().out.startswith("Recording 1 cases")


def test_record_unverified_only_says_when_nothing_is_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    done = make_case(tmp_path, name="done")
    done.path.write_text(
        done.path.read_text(encoding="utf-8")
        .replace("verified: false", "verified: true")
        .replace("recorded_at: null", "recorded_at: '2026-01-01T00:00:00Z'"),
        encoding="utf-8",
    )
    monkeypatch.setattr(corpus_cli, "get_dialect", lambda _name: PassthroughDialect())
    monkeypatch.setenv("BIGQUERY_PROJECT", "local-project")
    from google.cloud import bigquery

    monkeypatch.setattr(bigquery, "Client", ForbiddenClient)

    args = record_args(confirmed=True, path=str(tmp_path / "corpus"), unverified_only=True)
    assert corpus_cli.main(args) == 0
    assert capsys.readouterr().out.strip() == "no unverified cases to record"
