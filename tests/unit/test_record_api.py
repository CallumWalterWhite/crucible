"""`crucible corpus record-api`: the full call sequence against a fake session, proving redaction.

The fake responses deliberately contain the project id, an email, a server-minted job id, an
anonymous result table, an upload id and run-specific times; none may reach a fixture file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crucible.corpus.record_api import RecordApiError, Recorder, Redactor

PROJECT = "secret-project-123"
EMAIL = "someone@example.invalid"


class FakeResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self) -> Any:
        return self._body


class FakeSession:
    """Answers each REST call like BigQuery would, shape-wise, with identifying values inside."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url))
        body = kwargs.get("json") or {}
        job_ref = {"projectId": PROJECT, "jobId": "server_minted_job_42", "location": "EU"}
        done = {
            "kind": "bigquery#job",
            "etag": "abc==",
            "id": f"{PROJECT}:EU.server_minted_job_42",
            "selfLink": f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/jobs/x",
            "user_email": EMAIL,
            "jobReference": job_ref,
            "configuration": {
                "query": {
                    "destinationTable": {
                        "projectId": PROJECT,
                        "datasetId": "_0123abcd",
                        "tableId": "anon1234abcd",
                    }
                }
            },
            "statistics": {"creationTime": "1700000000000", "endTime": "1700000001000"},
            "status": {"state": "DONE"},
        }
        if "uploadType" in (kwargs.get("params") or {}):
            location = f"https://bigquery.googleapis.com/upload/bigquery/v2/projects/{PROJECT}/jobs?uploadType=resumable&upload_id=SECRETUPLOAD"
            return FakeResponse(200, None, {"Location": location})
        if method == "DELETE":
            return FakeResponse(204, None)
        if "/queries/" in url and method == "GET":
            params = kwargs.get("params") or {}
            result: dict[str, Any] = {
                "jobReference": job_ref,
                "jobComplete": True,
                "totalRows": "3",
                "rows": [{"f": [{"v": "1"}]}],
                "schema": {"fields": [{"name": "id", "type": "INTEGER", "mode": "NULLABLE"}]},
            }
            if params.get("maxResults") == "1" and "pageToken" not in params:
                result["pageToken"] = "PAGETOKEN123"
            return FakeResponse(200, result)
        if url.endswith("/queries"):
            return FakeResponse(
                200, {"jobReference": job_ref, "queryId": "q-1", "jobComplete": True}
            )
        if "/tables/" in url and method == "GET" and "no_such_table" in url:
            return FakeResponse(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": f"Not found: Table {PROJECT}:x.y",
                        "errors": [
                            {"reason": "notFound", "location": "query", "message": "Not found"}
                        ],
                    }
                },
            )
        if "/tables/" in url and method == "GET":
            return FakeResponse(
                200,
                {
                    "id": f"{PROJECT}:ds.t",
                    "schema": {"fields": [{"name": "id", "type": "INTEGER"}]},
                    "creationTime": "1700000000000",
                    "lastModifiedTime": "1700000000000",
                    "location": "EU",
                },
            )
        if "/datasets" in url and method == "GET" and url.endswith("/datasets"):
            return FakeResponse(
                200, {"datasets": [{"id": f"{PROJECT}:x"}], "nextPageToken": "NEXT"}
            )
        if (
            "/datasets/" in url
            and method == "GET"
            and "/tables" not in url
            and "/routines" not in url
        ):
            return FakeResponse(200, {"access": [{"role": "OWNER", "userByEmail": EMAIL}]})
        if body.get("jobReference") or method == "PUT":
            return FakeResponse(200, done)
        if method == "GET" and "/jobs/" in url:
            return FakeResponse(200, done)
        return FakeResponse(200, {"kind": "ok", "id": f"{PROJECT}:thing"})


@pytest.fixture
def recorded(tmp_path: Path) -> tuple[FakeSession, Path, list[str]]:
    session = FakeSession()
    recorder = Recorder(session, PROJECT, 104857600, tmp_path)
    recorder.run()
    return session, tmp_path, recorder.written


def test_every_call_is_recorded_and_the_dataset_is_deleted(recorded):
    session, _, written = recorded
    assert len(written) > 40
    assert session.calls[-1][0] == "DELETE"
    assert "deleteContents" not in session.calls[-1][1]  # passed as a query parameter
    names = {w.split("_", 1)[1].removesuffix(".json") for w in written}
    for expected in (
        "jobs_insert_select",
        "get_query_results_page_2",
        "jobs_query_job_creation_optional",
        "tables_get_anonymous",
        "load_baseline_upload",
        "load_bad_value_jobs_get",
        "jobs_insert_conflict",
    ):
        assert expected in names


def test_no_identifying_value_reaches_a_fixture(recorded):
    _, directory, written = recorded
    for name in written:
        text = (directory / name).read_text()
        for secret in (
            PROJECT,
            EMAIL,
            "server_minted_job_42",
            "_0123abcd",
            "anon1234abcd",
            "SECRETUPLOAD",
            "PAGETOKEN123",
            "1700000000000",
            "abc==",
        ):
            assert secret not in text, (name, secret)
        assert "@" not in text


def test_placeholders_are_consistent_and_error_locations_survive(recorded):
    _, directory, written = recorded
    by_name = {
        w.split("_", 1)[1].removesuffix(".json"): json.loads((directory / w).read_text())
        for w in written
    }
    select = by_name["jobs_get_select"]["body"]["jobReference"]
    assert select["projectId"] == "<project>"
    assert select["location"] == "<location>"
    assert select["jobId"].startswith("<job-")
    # The same job keeps the same placeholder across fixtures.
    assert by_name["jobs_get_dml"]["body"]["jobReference"]["jobId"] == select["jobId"]
    page_2 = by_name["get_query_results_page_2"]["request"]["params"]
    assert page_2["pageToken"].startswith("<page-token-")
    not_found = by_name["tables_get_not_found"]["body"]["error"]["errors"][0]
    assert not_found["location"] == "query"  # an error field, not a region
    upload = by_name["load_baseline_initiate"]["headers"]["Location"]
    assert "upload_id=<upload-id>" in upload
    assert "<project>" in upload


def test_the_final_check_refuses_leaks():
    redactor = Redactor("p-1", "ds_1")
    for leak in ('{"x": "a@b.c"}', '{"x": "p-1"}', '{"t": "ya29.abcdef"}', '{"h": "Bearer abc"}'):
        with pytest.raises(RecordApiError):
            redactor.check(leak)
    redactor.check('{"x": "<project>"}')


# BigQuery's reserved keywords (Lexical structure and syntax, "Reserved keywords"). An unquoted
# alias from this list is a syntax error in BigQuery; Crucible does not reject it yet (known
# divergence, corpus case syntax/reserved_keyword_alias_error).
RESERVED = set(
    [
        "ALL",
        "AND",
        "ANY",
        "ARRAY",
        "AS",
        "ASC",
        "ASSERT_ROWS_MODIFIED",
        "AT",
        "BETWEEN",
        "BY",
        "CASE",
        "CAST",
        "COLLATE",
        "CONTAINS",
        "CREATE",
        "CROSS",
        "CUBE",
        "CURRENT",
        "DEFAULT",
        "DEFINE",
        "DESC",
        "DISTINCT",
        "ELSE",
        "END",
        "ENUM",
        "ESCAPE",
        "EXCEPT",
        "EXCLUDE",
        "EXISTS",
        "EXTRACT",
        "FALSE",
        "FETCH",
        "FOLLOWING",
        "FOR",
        "FROM",
        "FULL",
        "GROUP",
        "GROUPING",
        "GROUPS",
        "HASH",
        "HAVING",
        "IF",
        "IGNORE",
        "IN",
        "INNER",
        "INTERSECT",
        "INTERVAL",
        "INTO",
        "IS",
        "JOIN",
        "LATERAL",
        "LEFT",
        "LIKE",
        "LIMIT",
        "LOOKUP",
        "MERGE",
        "NATURAL",
        "NEW",
        "NO",
        "NOT",
        "NULL",
        "NULLS",
        "OF",
        "ON",
        "OR",
        "ORDER",
        "OUTER",
        "OVER",
        "PARTITION",
        "PRECEDING",
        "PROTO",
        "QUALIFY",
        "RANGE",
        "RECURSIVE",
        "RESPECT",
        "RIGHT",
        "ROLLUP",
        "ROWS",
        "SELECT",
        "SET",
        "SOME",
        "STRUCT",
        "TABLESAMPLE",
        "THEN",
        "TO",
        "TREAT",
        "TRUE",
        "UNBOUNDED",
        "UNION",
        "UNNEST",
        "USING",
        "WHEN",
        "WHERE",
        "WINDOW",
        "WITH",
        "WITHIN",
    ]
)


def test_recorder_sql_uses_no_reserved_keyword_as_a_name():
    import re

    from crucible.corpus.record_api import ALL_TYPES_SQL, LOAD_SCHEMA

    # An alias is `AS name` followed by a comma or line end (not `AS ARRAY<...>` in a CAST).
    aliases = re.findall(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?=,|$)", ALL_TYPES_SQL, re.MULTILINE)
    assert "bt" in aliases
    names = {a.upper() for a in aliases} | {f["name"].upper() for f in LOAD_SCHEMA}
    assert not names & RESERVED


def test_a_failing_dry_run_stops_before_anything_is_created(tmp_path: Path):
    class DryRunFails(FakeSession):
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.calls.append((method, url))
            if (kwargs.get("json") or {}).get("dryRun"):
                return FakeResponse(
                    400, {"error": {"message": "Syntax error: Unexpected keyword BY"}}
                )
            return super().request(method, url, **kwargs)

    session = DryRunFails()
    with pytest.raises(RecordApiError, match="dry run"):
        Recorder(session, PROJECT, 1, tmp_path).run()
    assert len(session.calls) == 1  # only the free dry run
    assert list(tmp_path.iterdir()) == []
