"""`crucible corpus record-api`: record real BigQuery REST responses as wire-format fixtures.

The corpus measures values; these fixtures measure the wire: schema type names and modes, how
rows are encoded, job and paging fields, upload handshakes and error bodies. Server encoding is
compared against them field by field (Phase 3, B2).

Fixtures are committed to a public repository, so every response is redacted in code before a
file is written (`Redactor`), and a final check refuses to write anything that still contains an
email address, the project or dataset name, a job id, or something that looks like a token.
Safety matches `corpus record`: a bytes cap on every query job, an explicit spend flag, a
throwaway dataset with expiring tables and a label, deleted at the end.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API = "https://bigquery.googleapis.com/bigquery/v2"
UPLOAD = "https://bigquery.googleapis.com/upload/bigquery/v2"
DATASET_PREFIX = "crucible_api_"
TABLE_EXPIRATION_MS = 60 * 60 * 1000
LABELS = {"crucible-corpus": "1"}

# Keys whose values are run-specific or identifying; replaced wholesale (redaction rules).
PLACEHOLDER_KEYS: dict[str, str] = {
    "etag": "<etag>",
    "selfLink": "<self-link>",
    "creationTime": "<time>",
    "startTime": "<time>",
    "endTime": "<time>",
    "lastModifiedTime": "<time>",
    "expirationTime": "<time>",
    "location": "<location>",
    "user_email": "<email>",
    "principal_subject": "<principal>",
    "queryId": "<query-id>",
    "elapsedMs": "<duration>",
    "totalSlotMs": "<duration>",
    "finalExecutionDurationMs": "<duration>",
    "slotMs": "<duration>",
    "startMs": "<time>",
    "endMs": "<time>",
    "reservation_id": "<reservation>",
}
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
_UPLOAD_ID = re.compile(r"upload_id=[^&\s\"']+")
_TOKEN_LIKE = re.compile(r"(ya29\.[\w-]+|Bearer\s+\S+|AIza[0-9A-Za-z_-]{20,})")
# An email address. A bare `@` is allowed: query parameters are named `@name` in SQL.
EMAIL_LIKE = re.compile(r"[\w.+%-]+@[\w-]+(\.[\w-]+)+")
# Kept with their original case, so a "Location" header is not mistaken for a "location" field.
_KEEP_HEADERS = ("content-type", "location", "range")


class RecordApiError(Exception):
    """The recording cannot continue safely; nothing more is written."""


@dataclass
class Redactor:
    """Replaces identifying values with fixed placeholders, consistently across fixtures."""

    project: str
    dataset: str
    names: dict[str, str] = field(default_factory=dict[str, str])
    words: dict[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        self.names[self.project] = "<project>"
        self.names[self.dataset] = "<dataset>"

    def add(self, value: str, kind: str) -> str:
        """Register a run-specific name (job id, anonymous table, ...); return its placeholder."""
        if value not in self.names:
            count = sum(1 for p in self.names.values() if p.startswith(f"<{kind}-")) + 1
            self.names[value] = f"<{kind}-{count}>"
        return self.names[value]

    def register(self, obj: Any) -> None:
        """Register run-specific names a response introduces, before it is redacted."""
        if isinstance(obj, dict):
            for key, item in obj.items():  # pyright: ignore[reportUnknownVariableType]
                if isinstance(item, str):
                    if key == "jobId":
                        self.add(item, "job")
                    elif key == "location" and "reason" not in obj:
                        self.add_word(item, "<location>")
                    elif key in ("pageToken", "nextPageToken"):
                        self.add(item, "page-token")
                    elif key == "datasetId" and item.startswith("_"):
                        self.add(item, "anon-dataset")
                    elif key == "tableId" and item.startswith("anon"):
                        self.add(item, "anon-table")
                else:
                    self.register(item)
        elif isinstance(obj, list):
            for item in obj:  # pyright: ignore[reportUnknownVariableType]
                self.register(item)

    def add_word(self, value: str, placeholder: str) -> None:
        """A short value (a location such as US) replaced only as a whole word in text."""
        self.words[value] = placeholder

    def text(self, value: str) -> str:
        for original in sorted(self.names, key=len, reverse=True):
            value = value.replace(original, self.names[original])
        for word, placeholder in self.words.items():
            value = re.sub(
                rf"(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_-])", placeholder, value
            )
        value = _UPLOAD_ID.sub("upload_id=<upload-id>", value)
        return _EMAIL.sub("<email>", value)

    def value(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, item in obj.items():  # pyright: ignore[reportUnknownVariableType]
                name = str(key)  # pyright: ignore[reportUnknownArgumentType]
                # An error object's "location" names the failing field ("query"), not a region.
                is_error_field = name == "location" and "reason" in obj
                if (
                    name in PLACEHOLDER_KEYS
                    and not isinstance(item, dict | list)
                    and not is_error_field
                ):
                    out[name] = PLACEHOLDER_KEYS[name]
                else:
                    out[name] = self.value(item)
            return out
        if isinstance(obj, list):
            return [self.value(item) for item in obj]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def check(self, serialized: str) -> None:
        """Refuse to write a fixture that still identifies anything (hard requirement)."""
        if EMAIL_LIKE.search(serialized):
            raise RecordApiError("redaction left an email address in a fixture")
        for original in self.names:
            if original in serialized:
                raise RecordApiError("redaction left a project, dataset or job name in a fixture")
        if _TOKEN_LIKE.search(serialized):
            raise RecordApiError("a fixture contains something that looks like a token")


Session = Any  # google.auth.transport.requests.AuthorizedSession (untyped)


@dataclass
class Recorder:
    session: Session
    project: str
    max_bytes_billed: int
    out_dir: Path
    dataset: str = field(default_factory=lambda: f"{DATASET_PREFIX}{uuid.uuid4().hex[:12]}")
    redactor: Redactor = field(init=False)
    written: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        self.redactor = Redactor(self.project, self.dataset)

    # --- HTTP -------------------------------------------------------------------------------
    def call(
        self,
        name: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] | None = (200,),
    ) -> Any:
        """Send, record and return (payload, headers). expect=None records any status: used for
        error calls, whose exact status is part of what is being measured."""
        response = self.session.request(
            method, url, params=params, json=body, data=data, headers=headers, timeout=120
        )
        status = int(response.status_code)
        try:
            payload: Any = response.json() if response.content else None
        except ValueError:
            payload = response.text
        if expect is not None and status not in expect:
            raise RecordApiError(f"{name}: expected HTTP {expect}, got {status}")
        # An upload's CSV is part of the fixture, so a replay can send the same bytes.
        sent = body if data is None else data.decode("utf-8")
        self.save(name, method, url, params, sent, status, dict(response.headers), payload)
        return payload, dict(response.headers)

    def save(
        self,
        name: str,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        body: Any,
        status: int,
        headers: dict[str, str],
        payload: Any,
    ) -> None:
        kept = {k: v for k, v in headers.items() if k.lower() in _KEEP_HEADERS}
        self.redactor.register(payload)
        fixture = self.redactor.value(
            {
                "request": {
                    "method": method,
                    "path": url.replace(API, "").replace(UPLOAD, "/upload"),
                    "params": params or {},
                    "body": body,
                },
                "status": status,
                "headers": kept,
                "body": payload,
            }
        )
        serialized = json.dumps(fixture, indent=2, sort_keys=False) + "\n"
        self.redactor.check(serialized)
        path = self.out_dir / f"{len(self.written):02d}_{name}.json"
        path.write_text(serialized, encoding="utf-8")
        self.written.append(path.name)

    # --- helpers ----------------------------------------------------------------------------
    def job_id(self, purpose: str) -> str:
        job = f"crucible_api_{purpose}_{uuid.uuid4().hex[:12]}"
        self.redactor.add(job, "job")
        return job

    def query_config(self, sql: str, **extra: Any) -> dict[str, Any]:
        return {
            "query": {
                "query": sql,
                "useLegacySql": False,
                "maximumBytesBilled": str(self.max_bytes_billed),
                "defaultDataset": {"projectId": self.project, "datasetId": self.dataset},
                # A cached answer is not a fresh query's answer (statistics differ).
                "useQueryCache": False,
                **extra,
            }
        }

    def insert_job(self, name: str, purpose: str, configuration: dict[str, Any]) -> str:
        job = self.job_id(purpose)
        body = {
            "jobReference": {"projectId": self.project, "jobId": job},
            "configuration": configuration,
        }
        self.call(name, "POST", f"{API}/projects/{self.project}/jobs", body=body)
        return job

    def wait(self, job: str) -> Any:
        deadline = time.monotonic() + 300
        while True:
            response = self.session.request(
                "GET", f"{API}/projects/{self.project}/jobs/{job}", timeout=60
            )
            payload: Any = response.json()
            if payload.get("status", {}).get("state") == "DONE":
                return payload
            if time.monotonic() > deadline:
                raise RecordApiError("a job did not finish within 5 minutes")
            time.sleep(1)

    def wait_ok(self, job: str, purpose: str) -> Any:
        """Wait for a job the rest of the run depends on; stop early (and cheaply) if it failed."""
        payload = self.wait(job)
        error = payload.get("status", {}).get("errorResult")
        if error:
            raise RecordApiError(
                f"{purpose} job failed: {self.redactor.text(str(error.get('message')))}"
            )
        return payload

    def note_anonymous(self, job_payload: Any) -> None:
        table = job_payload.get("configuration", {}).get("query", {}).get("destinationTable")
        if table:
            self.redactor.add(table["datasetId"], "anon-dataset")
            self.redactor.add(table["tableId"], "anon-table")

    # --- the fixed set of calls --------------------------------------------------------------
    def preflight(self) -> None:
        """A dry run of the all-types query: free; fails before anything is created or billed."""
        response = self.session.request(
            "POST",
            f"{API}/projects/{self.project}/queries",
            json={"query": ALL_TYPES_SQL, "useLegacySql": False, "dryRun": True},
            timeout=120,
        )
        if int(response.status_code) != 200:
            message = response.json().get("error", {}).get("message", response.text)
            raise RecordApiError(
                f"dry run of the all-types query failed: {self.redactor.text(str(message))}"
            )

    def run(self) -> None:
        p, d = self.project, self.dataset
        datasets = f"{API}/projects/{p}/datasets"
        self.preflight()
        for stale in self.out_dir.glob("[0-9][0-9]*_*.json"):
            stale.unlink()  # regenerated as a set: a partial run must not mix with an older one
        self.call(
            "datasets_insert",
            "POST",
            datasets,
            body={
                "datasetReference": {"projectId": p, "datasetId": d},
                "defaultTableExpirationMs": str(TABLE_EXPIRATION_MS),
                "labels": LABELS,
            },
        )
        try:
            self.record_all(p, d, datasets)
        finally:
            self.session.request(
                "DELETE", f"{datasets}/{d}", params={"deleteContents": "true"}, timeout=120
            )

    def record_all(self, p: str, d: str, datasets: str) -> None:
        tables = f"{datasets}/{d}/tables"
        ctas = self.insert_job(
            "jobs_insert_ctas",
            "ctas",
            self.query_config(f"CREATE TABLE all_types AS {ALL_TYPES_SQL}"),
        )
        self.wait_ok(ctas, "CREATE TABLE AS SELECT")
        self.call("jobs_get_ctas", "GET", f"{API}/projects/{p}/jobs/{ctas}")

        select = self.insert_job(
            "jobs_insert_select", "select", self.query_config(f"{ALL_TYPES_SQL} ORDER BY id")
        )
        done = self.wait_ok(select, "SELECT")
        self.note_anonymous(done)
        self.call("jobs_get_select", "GET", f"{API}/projects/{p}/jobs/{select}")
        results = f"{API}/projects/{p}/queries/{select}"
        self.call("get_query_results", "GET", results)
        self.call(
            "get_query_results_int64_timestamp",
            "GET",
            results,
            params={"formatOptions.useInt64Timestamp": "true"},
        )
        first, _ = self.call("get_query_results_page_1", "GET", results, params={"maxResults": "1"})
        token = first.get("pageToken")
        if not token:
            raise RecordApiError("no pageToken with maxResults=1 over 3 rows")
        self.redactor.add(token, "page-token")
        self.call(
            "get_query_results_page_2",
            "GET",
            results,
            params={"maxResults": "1", "pageToken": token},
        )
        self.call("get_query_results_start_index", "GET", results, params={"startIndex": "2"})
        self.call(
            "get_query_results_wait",
            "GET",
            results,
            params={"maxResults": "0", "timeoutMs": "10000"},
        )

        query = self.query_config("SELECT 1 AS a, 'x' AS b")["query"]
        self.call("jobs_query", "POST", f"{API}/projects/{p}/queries", body=query)
        self.call(
            "jobs_query_job_creation_optional",
            "POST",
            f"{API}/projects/{p}/queries",
            body={**query, "jobCreationMode": "JOB_CREATION_OPTIONAL"},
        )

        table, _ = self.call("tables_get", "GET", f"{tables}/all_types")
        anonymous = done["configuration"]["query"]["destinationTable"]
        self.call(
            "tables_get_anonymous",
            "GET",
            f"{API}/projects/{p}/datasets/{anonymous['datasetId']}/tables/{anonymous['tableId']}",
        )
        self.call("tables_list", "GET", tables)
        self.call("datasets_get", "GET", f"{datasets}/{d}")
        self.call("datasets_list", "GET", datasets, params={"filter": "labels.crucible-corpus:1"})
        self.call("routines_list", "GET", f"{datasets}/{d}/routines")

        # tabledata.list: a stored table and an anonymous result table, with paging.
        data = f"{tables}/all_types/data"
        self.call("tabledata_list", "GET", data)
        first, _ = self.call("tabledata_list_page_1", "GET", data, params={"maxResults": "1"})
        token = first.get("pageToken")
        if not token:
            raise RecordApiError("no pageToken from tabledata.list with maxResults=1 over 3 rows")
        self.redactor.add(token, "page-token")
        self.call(
            "tabledata_list_page_2",
            "GET",
            data,
            params={"maxResults": "1", "pageToken": token},
        )
        self.call("tabledata_list_start_index", "GET", data, params={"startIndex": "2"})
        self.call(
            "tabledata_list_int64_timestamp",
            "GET",
            data,
            params={"formatOptions.useInt64Timestamp": "true"},
        )
        self.call(
            "tabledata_list_anonymous",
            "GET",
            f"{API}/projects/{p}/datasets/{anonymous['datasetId']}/tables/"
            f"{anonymous['tableId']}/data",
        )

        dml = self.insert_job(
            "jobs_insert_dml",
            "dml",
            self.query_config("INSERT INTO all_types (id, s) VALUES (10, 'a'), (11, 'b')"),
        )
        self.wait(dml)
        self.call("jobs_get_dml", "GET", f"{API}/projects/{p}/jobs/{dml}")

        for name, csv_text in LOAD_CASES.items():
            self.load(name, csv_text)

        fields = [*table["schema"]["fields"], {"name": "added", "type": "STRING"}]
        self.call(
            "tables_patch_add_column",
            "PATCH",
            f"{tables}/all_types",
            body={"schema": {"fields": fields}},
            expect=None,
        )

        runtime = self.insert_job(
            "jobs_insert_runtime_error", "error", self.query_config("SELECT 1 / 0 AS v")
        )
        self.wait(runtime)
        self.call("jobs_get_runtime_error", "GET", f"{API}/projects/{p}/jobs/{runtime}")
        self.call(
            "get_query_results_runtime_error",
            "GET",
            f"{API}/projects/{p}/queries/{runtime}",
            expect=None,
        )
        self.call(
            "jobs_query_syntax_error",
            "POST",
            f"{API}/projects/{p}/queries",
            body=self.query_config("SELEC 1")["query"],
            expect=None,
        )
        self.call("tables_get_not_found", "GET", f"{tables}/no_such_table", expect=None)
        self.call(
            "jobs_insert_conflict",
            "POST",
            f"{API}/projects/{p}/jobs",
            body={
                "jobReference": {"projectId": p, "jobId": select},
                "configuration": self.query_config("SELECT 1"),
            },
            expect=None,
        )
        self.call(
            "datasets_insert_conflict",
            "POST",
            datasets,
            body={"datasetReference": {"projectId": p, "datasetId": d}},
            expect=None,
        )
        # Partitioned and clustered tables, as tables.get and tables.list report them. Last, so
        # the job placeholders of every earlier call keep their numbers.
        for name, sql in PARTITIONED_TABLES.items():
            job = self.insert_job(f"jobs_insert_{name}", name, self.query_config(sql))
            self.wait_ok(job, f"CREATE TABLE {name}")
            self.call(f"tables_get_{name}", "GET", f"{tables}/{name}")
        self.call("tables_list_partitioned", "GET", tables)
        self.record_dml(p, tables)
        self.record_insert_all(p, tables)
        self.record_information_schema(p, self.dataset)
        self.call("tables_delete", "DELETE", f"{tables}/all_types", expect=(200, 204))
        self.record_metadata(p, tables)
        self.record_table_options(p)
        self.record_dry_runs(p)
        self.record_query_parameters(p)
        self.record_scripts_and_copies(p)

    def record_metadata(self, p: str, tables: str) -> None:
        """Table description, labels, field descriptions and REQUIRED columns: as tables.insert
        and tables.patch send them, as tables.get returns them, and as ALTER ... SET OPTIONS sets
        them. Last, after the dataset's other tables."""
        fields = [
            {"name": "k", "type": "INTEGER", "mode": "REQUIRED", "description": "the key"},
            {"name": "v", "type": "STRING", "description": "the value"},
        ]
        self.call(
            "tables_insert_metadata",
            "POST",
            tables,
            body={
                "tableReference": {"projectId": p, "datasetId": self.dataset, "tableId": "meta_t"},
                "schema": {"fields": fields},
                "description": "a table with metadata",
                "labels": {"team": "data"},
            },
        )
        self.call("tables_get_metadata", "GET", f"{tables}/meta_t")
        self.call(
            "tables_patch_metadata",
            "PATCH",
            f"{tables}/meta_t",
            body={
                "description": "patched",
                "labels": {"team": "dbt"},
                "schema": {"fields": [fields[0], {**fields[1], "description": "patched value"}]},
            },
        )
        self.call("tables_get_patched", "GET", f"{tables}/meta_t")
        alter = self.insert_job(
            "jobs_insert_alter_options",
            "alter_options",
            self.query_config(
                "ALTER TABLE meta_t SET OPTIONS (description = 'altered', labels = [('a', 'b')])"
            ),
        )
        self.wait_ok(alter, "ALTER TABLE SET OPTIONS")
        self.call("jobs_get_alter_options", "GET", f"{API}/projects/{p}/jobs/{alter}")
        self.call("tables_get_altered", "GET", f"{tables}/meta_t")
        self.call(
            "jobs_query_null_into_required",
            "POST",
            f"{API}/projects/{p}/queries",
            body=self.query_config("INSERT INTO meta_t (k, v) VALUES (NULL, 'x')")["query"],
            expect=None,
        )

    def record_dml(self, p: str, tables: str) -> None:
        """UPDATE, DELETE and TRUNCATE TABLE: statistics, results, and the missing-WHERE errors."""
        setup = self.insert_job(
            "jobs_insert_dml_setup",
            "dml_setup",
            self.query_config("CREATE TABLE dml_t AS SELECT k FROM UNNEST([1, 2, 3]) AS k"),
        )
        self.wait_ok(setup, "CREATE TABLE dml_t")
        for kind, sql in (
            ("update", "UPDATE dml_t SET k = k + 10 WHERE k >= 2"),
            ("delete", "DELETE FROM dml_t WHERE k = 1"),
            ("truncate", "TRUNCATE TABLE dml_t"),
        ):
            job = self.insert_job(f"jobs_insert_{kind}", kind, self.query_config(sql))
            self.wait_ok(job, kind.upper())
            self.call(f"jobs_get_{kind}", "GET", f"{API}/projects/{p}/jobs/{job}")
            self.call(f"get_query_results_{kind}", "GET", f"{API}/projects/{p}/queries/{job}")
        self.call("tables_get_after_truncate", "GET", f"{tables}/dml_t")
        for kind, sql in (
            ("update", "UPDATE dml_t SET k = 1"),
            ("delete", "DELETE FROM dml_t"),
        ):
            self.call(
                f"jobs_query_{kind}_without_where",
                "POST",
                f"{API}/projects/{p}/queries",
                body=self.query_config(sql)["query"],
                expect=None,
            )

    def record_insert_all(self, p: str, tables: str) -> None:
        """tabledata.insertAll: each spelling stream.py accepts, a bad row with and without
        skipInvalidRows, ignoreUnknownValues, then the rows as stored and DML over the buffer."""
        self.call(
            "tables_insert_streamed",
            "POST",
            tables,
            body={
                "tableReference": {
                    "projectId": p,
                    "datasetId": self.dataset,
                    "tableId": "streamed",
                },
                "schema": {"fields": STREAMED_FIELDS},
            },
        )
        url = f"{tables}/streamed/insertAll"
        for name, rows, flags in INSERT_ALL_CALLS:
            self.call(f"insert_all_{name}", "POST", url, body={"rows": rows, **flags}, expect=None)
        self.call("tables_get_streamed", "GET", f"{tables}/streamed")
        select = self.insert_job(
            "jobs_insert_select_streamed",
            "select_streamed",
            self.query_config(f"SELECT {STREAMED_COLUMNS} FROM streamed ORDER BY k, f"),
        )
        self.wait_ok(select, "SELECT streamed")
        self.call("get_query_results_streamed", "GET", f"{API}/projects/{p}/queries/{select}")
        self.call(
            "jobs_query_update_streamed",
            "POST",
            f"{API}/projects/{p}/queries",
            body=self.query_config("UPDATE streamed SET s = 'u' WHERE TRUE")["query"],
            expect=None,
        )

    def record_information_schema(self, p: str, d: str) -> None:
        """The __TABLES__ and INFORMATION_SCHEMA columns dbt docs generate reads (catalog.sql)."""
        setup = self.insert_job(
            "jobs_insert_options_table",
            "options_table",
            self.query_config(
                "CREATE TABLE opts (a INT64, r STRUCT<x INT64, ys ARRAY<STRUCT<z STRING>>>) "
                "CLUSTER BY a OPTIONS (description = 'Say \"hi\"', labels = [('team', 'data')])"
            ),
        )
        self.wait_ok(setup, "CREATE TABLE opts")
        names = "('all_types', 'opts', 'partitioned_day', 'partitioned_range', 'streamed')"
        for name, sql in (
            (
                "tables",
                f"SELECT project_id, dataset_id, table_id, row_count, type "
                f"FROM `{p}`.`{d}`.__TABLES__ WHERE table_id IN {names} ORDER BY table_id",
            ),
            (
                "columns",
                f"SELECT table_catalog, table_schema, table_name, column_name, ordinal_position, "
                f"is_nullable, data_type, is_partitioning_column, clustering_ordinal_position "
                f"FROM `{p}`.`{d}`.INFORMATION_SCHEMA.COLUMNS "
                f"WHERE table_name IN {names} ORDER BY table_name, ordinal_position",
            ),
            (
                "column_field_paths",
                f"SELECT table_name, column_name, field_path, data_type, description "
                f"FROM `{p}`.`{d}`.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS "
                f"WHERE table_name IN {names} ORDER BY table_name, field_path",
            ),
            (
                "materialized_views",
                f"SELECT COUNT(*) AS n FROM `{p}`.`{d}`.INFORMATION_SCHEMA.MATERIALIZED_VIEWS",
            ),
            (
                "columns_region",
                f"SELECT table_name, column_name FROM `{p}`.`region-us`.INFORMATION_SCHEMA.COLUMNS "
                f"WHERE table_schema = '{d}' AND table_name = 'opts' ORDER BY ordinal_position",
            ),
        ):
            self.call(
                f"jobs_query_information_schema_{name}",
                "POST",
                f"{API}/projects/{p}/queries",
                body=self.query_config(sql)["query"],
                expect=None,
            )

    def record_table_options(self, p: str) -> None:
        """INFORMATION_SCHEMA.TABLE_OPTIONS. In the main dataset every table has an
        expiration_timestamp option (its default table expiration), which Crucible refuses, so
        that call runs last; a dataset without a default expiration gives the comparable answer."""
        d = self.dataset
        plain = f"{d}_noexp"
        datasets = f"{API}/projects/{p}/datasets"
        self.call(
            "datasets_insert_no_expiration",
            "POST",
            datasets,
            body={"datasetReference": {"projectId": p, "datasetId": plain}, "labels": LABELS},
        )
        try:
            for name, sql in (
                (
                    "opts",
                    f"CREATE TABLE `{plain}`.opts (a INT64) CLUSTER BY a "
                    "OPTIONS (description = 'Say \"hi\"', labels = [('team', 'data')])",
                ),
                (
                    "parts",
                    f"CREATE TABLE `{plain}`.parts PARTITION BY d AS SELECT DATE '2024-01-02' AS d",
                ),
            ):
                job = self.insert_job(
                    f"jobs_insert_{name}_no_expiration",
                    f"{name}_no_expiration",
                    self.query_config(sql),
                )
                self.wait_ok(job, f"CREATE TABLE {name}")
            self.call(
                "jobs_query_information_schema_table_options_no_expiration",
                "POST",
                f"{API}/projects/{p}/queries",
                body=self.query_config(
                    "SELECT table_name, option_name, option_type, option_value "
                    f"FROM `{p}`.`{plain}`.INFORMATION_SCHEMA.TABLE_OPTIONS "
                    "ORDER BY table_name, option_name"
                )["query"],
                expect=None,
            )
        finally:
            self.session.request(
                "DELETE", f"{datasets}/{plain}", params={"deleteContents": "true"}, timeout=120
            )
        names = "('opts', 'partitioned_day', 'partitioned_range', 'streamed')"
        self.call(
            "jobs_query_information_schema_table_options",
            "POST",
            f"{API}/projects/{p}/queries",
            body=self.query_config(
                "SELECT table_name, option_name, option_type, option_value "
                f"FROM `{p}`.`{d}`.INFORMATION_SCHEMA.TABLE_OPTIONS "
                f"WHERE table_name IN {names} ORDER BY table_name, option_name"
            )["query"],
            expect=None,
        )

    def record_dry_runs(self, p: str) -> None:
        """dryRun on jobs.insert and jobs.query: a query, a statement and a plan-time error.
        Last, so earlier placeholders keep their numbers."""
        for name, sql in (
            ("select", "SELECT k, s, 1 / 0 AS z FROM streamed"),
            ("ddl", "CREATE TABLE dry_t (a INT64)"),
            ("missing_table", "SELECT * FROM no_such_table"),
        ):
            job = self.job_id(f"dry_{name}")
            body = {
                "jobReference": {"projectId": p, "jobId": job},
                "configuration": {**self.query_config(sql), "dryRun": True},
            }
            self.call(
                f"jobs_insert_dry_run_{name}",
                "POST",
                f"{API}/projects/{p}/jobs",
                body=body,
                expect=None,
            )
        self.call(
            "jobs_query_dry_run",
            "POST",
            f"{API}/projects/{p}/queries",
            body={**self.query_config("SELECT k, s FROM streamed")["query"], "dryRun": True},
        )

    def record_query_parameters(self, p: str) -> None:
        """queryParameters in the spellings the client sends (params.py): each scalar type,
        positional, ARRAY and STRUCT, a missing parameter, and the same through jobs.insert."""
        for name, mode, sql, parameters in QUERY_PARAMETER_CALLS:
            self.call(
                f"jobs_query_parameters_{name}",
                "POST",
                f"{API}/projects/{p}/queries",
                body={
                    **self.query_config(sql)["query"],
                    "parameterMode": mode,
                    "queryParameters": parameters,
                },
                expect=None,
            )
        job = self.insert_job(
            "jobs_insert_parameters",
            "parameters",
            self.query_config(
                "SELECT @k * 2 AS v",
                parameterMode="NAMED",
                queryParameters=[_param("k", "INT64", "21")],
            ),
        )
        self.wait(job)
        self.call("get_query_results_parameters", "GET", f"{API}/projects/{p}/queries/{job}")

    def record_scripts_and_copies(self, p: str) -> None:
        """A script job (DECLARE, SET, SELECT: its SCRIPT statistics and results), and copy jobs:
        a whole table, and one partition with WRITE_TRUNCATE (dbt's copy_partitions)."""
        script = self.insert_job(
            "jobs_insert_script",
            "script",
            self.query_config(
                "DECLARE ks ARRAY<INT64>; "
                "SET (ks) = (SELECT AS STRUCT ARRAY_AGG(DISTINCT k IGNORE NULLS) FROM streamed); "
                "SELECT ARRAY_LENGTH(ks) AS n"
            ),
        )
        self.wait(script)
        self.call("jobs_get_script", "GET", f"{API}/projects/{p}/jobs/{script}")
        self.call("get_query_results_script", "GET", f"{API}/projects/{p}/queries/{script}")
        for name, sql in (
            (
                "copy_target",
                "CREATE TABLE copy_target PARTITION BY d AS "
                "SELECT DATE '2024-01-01' AS d, 1 AS k UNION ALL SELECT DATE '2024-01-02', 2",
            ),
            (
                "copy_source",
                "CREATE TABLE copy_source PARTITION BY d AS SELECT DATE '2024-01-02' AS d, 20 AS k",
            ),
        ):
            job = self.insert_job(f"jobs_insert_{name}", name, self.query_config(sql))
            self.wait_ok(job, f"CREATE TABLE {name}")
        for name, source, destination, extra in (
            ("table", "copy_target", "copy_whole", {}),
            (
                "partition",
                "copy_source$20240102",
                "copy_target$20240102",
                {"writeDisposition": "WRITE_TRUNCATE"},
            ),
        ):
            job = self.job_id(f"copy_{name}")

            def ref(table: str) -> dict[str, str]:
                return {"projectId": p, "datasetId": self.dataset, "tableId": table}

            body: dict[str, Any] = {
                "jobReference": {"projectId": p, "jobId": job},
                "configuration": {
                    "copy": {
                        "sourceTables": [ref(source)],
                        "destinationTable": ref(destination),
                        **extra,
                    }
                },
            }
            self.call(f"jobs_insert_copy_{name}", "POST", f"{API}/projects/{p}/jobs", body=body)
            self.wait(job)
            self.call(f"jobs_get_copy_{name}", "GET", f"{API}/projects/{p}/jobs/{job}")
        target = self.insert_job(
            "jobs_insert_copy_check",
            "copy_check",
            self.query_config("SELECT d, k FROM copy_target ORDER BY d"),
        )
        self.wait(target)
        self.call("get_query_results_copy_check", "GET", f"{API}/projects/{p}/queries/{target}")

    def load(self, name: str, csv_text: str) -> None:
        """A dbt-seed-style load: resumable upload of a CSV with an explicit schema."""
        job = self.job_id(f"load_{name}")
        configuration = {
            "load": {
                "destinationTable": {
                    "projectId": self.project,
                    "datasetId": self.dataset,
                    "tableId": f"load_{name}",
                },
                "schema": {"fields": LOAD_SCHEMA},
                "sourceFormat": "CSV",
                "skipLeadingRows": 1,
                "writeDisposition": "WRITE_TRUNCATE",
            }
        }
        _, headers = self.call(
            f"load_{name}_initiate",
            "POST",
            f"{UPLOAD}/projects/{self.project}/jobs",
            params={"uploadType": "resumable"},
            body={
                "jobReference": {"projectId": self.project, "jobId": job},
                "configuration": configuration,
            },
        )
        location = {k.lower(): v for k, v in headers.items()}.get("location")
        if not location:
            raise RecordApiError("resumable upload returned no Location header")
        payload = csv_text.encode()
        self.call(
            f"load_{name}_upload",
            "PUT",
            location,
            data=payload,
            headers={"Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"},
        )
        self.wait(job)
        self.call(f"load_{name}_jobs_get", "GET", f"{API}/projects/{self.project}/jobs/{job}")
        select = self.insert_job(
            f"load_{name}_select_insert",
            f"load_{name}_select",
            self.query_config(f"SELECT * FROM load_{name} ORDER BY id"),
        )
        result = self.wait(select)
        if result.get("status", {}).get("errorResult") is None:
            self.call(
                f"load_{name}_query_results",
                "GET",
                f"{API}/projects/{self.project}/queries/{select}",
            )


# Every ADR 0014 type, NULLs, nested STRUCT/ARRAY and NUMERIC edge values; three rows for paging.
ALL_TYPES_SQL = """
SELECT 1 AS id, CAST(42 AS INT64) AS i, 1.5 AS f, NUMERIC '123.456000001' AS n, TRUE AS b,
  'x' AS s, b'\\x01\\xff' AS bt, DATE '2024-02-29' AS d,
  DATETIME '2024-01-02 03:04:05.123456' AS dt, TIME '04:05:06.5' AS t,
  TIMESTAMP '2024-01-02 03:04:05.123456+00' AS ts, JSON '{"a": [1, null]}' AS j,
  [1, 2] AS arr, STRUCT(1 AS x, ['a', 'b'] AS y) AS st, [STRUCT(1 AS k, 'v' AS w)] AS arr_st,
  INTERVAL 1 DAY AS iv
UNION ALL
SELECT 2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  CAST([] AS ARRAY<INT64>), CAST(NULL AS STRUCT<x INT64, y ARRAY<STRING>>),
  CAST([] AS ARRAY<STRUCT<k INT64, w STRING>>), NULL
UNION ALL
SELECT 3, -9223372036854775808, CAST('nan' AS FLOAT64), NUMERIC '-0.000000001', FALSE, '',
  b'', DATE '0001-01-01', DATETIME '9999-12-31 23:59:59.999999', TIME '00:00:00',
  TIMESTAMP '1970-01-01 00:00:00+00', JSON 'null', [-1],
  STRUCT(CAST(NULL AS INT64) AS x, CAST([] AS ARRAY<STRING>) AS y),
  CAST([] AS ARRAY<STRUCT<k INT64, w STRING>>), INTERVAL -3 MONTH
"""

# CREATE TABLE AS SELECT of literals (bills nothing); one per partitioning kind.
PARTITIONED_TABLES: dict[str, str] = {
    "partitioned_day": "CREATE TABLE partitioned_day PARTITION BY d CLUSTER BY s, n "
    "AS SELECT DATE '2024-01-02' AS d, 'x' AS s, 1 AS n",
    "partitioned_hour": "CREATE TABLE partitioned_hour PARTITION BY TIMESTAMP_TRUNC(ts, HOUR) "
    "AS SELECT TIMESTAMP '2024-01-02 03:04:05+00' AS ts",
    "partitioned_month": "CREATE TABLE partitioned_month PARTITION BY DATE_TRUNC(d, MONTH) "
    "AS SELECT DATE '2024-01-02' AS d",
    "partitioned_range": "CREATE TABLE partitioned_range "
    "PARTITION BY RANGE_BUCKET(n, GENERATE_ARRAY(0, 100, 10)) AS SELECT 5 AS n",
}

LOAD_SCHEMA: list[dict[str, str]] = [
    {"name": "id", "type": "INTEGER"},
    {"name": "b", "type": "BOOLEAN"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "d", "type": "DATE"},
    {"name": "n", "type": "NUMERIC"},
    {"name": "f", "type": "FLOAT"},
    {"name": "s", "type": "STRING"},
]

# CSV load rules can differ from CAST rules: boolean spellings, timestamp forms, empty fields,
# quoting. A baseline file, then one single-row file per uncertain spelling. Every other field in a
# case holds a value the baseline proved loads (the first recording used a date-only TIMESTAMP in
# the other fields, which BigQuery rejects, so those cases measured nothing else).
_HEADER = "id,b,ts,d,n,f,s\n"
_TS = "2024-01-02 03:04:05"


# tabledata.insertAll (stream.py): one column per scalar type, an array and a record.
STREAMED_FIELDS = [
    {"name": "k", "type": "INTEGER"},
    {"name": "f", "type": "FLOAT"},
    {"name": "n", "type": "NUMERIC"},
    {"name": "b", "type": "BOOLEAN"},
    {"name": "s", "type": "STRING"},
    {"name": "y", "type": "BYTES"},
    {"name": "d", "type": "DATE"},
    {"name": "dt", "type": "DATETIME"},
    {"name": "tm", "type": "TIME"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "j", "type": "JSON"},
    {"name": "a", "type": "INTEGER", "mode": "REPEATED"},
    {
        "name": "r",
        "type": "RECORD",
        "fields": [{"name": "x", "type": "INTEGER"}, {"name": "z", "type": "STRING"}],
    },
]
STREAMED_COLUMNS = "k, f, n, b, s, y, d, dt, tm, ts, TO_JSON_STRING(j) AS j, a, r"
_CLIENT_ROW = {  # what insert_rows sends (google/cloud/bigquery/_helpers.py)
    "k": "1",
    "f": 1.5,
    "n": "1.25",
    "b": "true",
    "s": "x",
    "y": "Af8=",
    "d": "2024-02-29",
    "dt": "2024-01-02T03:04:05.123456",
    "tm": "04:05:06.500000",
    "ts": "2024-01-02T03:04:05.123456Z",
    "j": '{"a": 1}',
    "a": ["1", "2"],
    "r": {"x": "3", "z": "q"},
}
INSERT_ALL_CALLS: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = [
    ("client_spellings", [{"json": _CLIENT_ROW}], {}),
    # What insert_rows_json passes through: JSON numbers and booleans, epoch seconds, no array.
    (
        "plain_json",
        [{"json": {"k": 2, "f": 2, "n": 3, "b": False, "ts": 1704164645.5, "s": "plain"}}],
        {},
    ),
    (
        "non_finite",
        [
            {"json": {"k": 3, "f": f}}
            for f in ("nan", "inf", "-inf", "NaN", "Infinity", "-Infinity")
        ],
        {},
    ),
    ("bad_row", [{"json": {"k": 4}}, {"json": {"k": "abc"}}, {"json": {"k": 5, "nope": 1}}], {}),
    ("skip_invalid", [{"json": {"k": 6}}, {"json": {"k": "abc"}}], {"skipInvalidRows": True}),
    ("ignore_unknown", [{"json": {"k": 7, "nope": 1}}], {"ignoreUnknownValues": True}),
]


def _param(name: str | None, kind: str, value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"parameterType": {"type": kind}, "parameterValue": {"value": value}}
    return out | ({"name": name} if name is not None else {})


# Spellings google-cloud-bigquery sends (_helpers.py, _SCALAR_VALUE_TO_JSON_PARAM).
QUERY_PARAMETER_CALLS: list[tuple[str, str, str, list[dict[str, Any]]]] = [
    (
        "scalars",
        "NAMED",
        "SELECT @i + 1 AS i, @f AS f, @n AS n, @b AS b, @s AS s, @y AS y, @d AS d, @dt AS dt, "
        "@tm AS tm, @ts AS ts, TO_JSON_STRING(@j) AS j, @nul AS nul",
        [
            _param("i", "INT64", "5"),
            _param("f", "FLOAT64", 1.5),
            _param("n", "NUMERIC", "1.25"),
            _param("b", "BOOL", "true"),
            _param("s", "STRING", "it's"),
            _param("y", "BYTES", "Af8="),
            _param("d", "DATE", "2024-02-29"),
            _param("dt", "DATETIME", "2024-01-02T03:04:05.123456"),
            _param("tm", "TIME", "04:05:06.500000"),
            _param("ts", "TIMESTAMP", "2024-01-02 03:04:05.123456+00:00"),
            _param("j", "JSON", '{"a": 1}'),
            _param("nul", "STRING", None),
        ],
    ),
    (
        "positional",
        "POSITIONAL",
        "SELECT ? * 10 AS v, ? AS s",
        [_param(None, "INT64", "2"), _param(None, "STRING", "b")],
    ),
    (
        "array_struct",
        "NAMED",
        "SELECT ARRAY_LENGTH(@a) AS l, @st.x AS x",
        [
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
        ],
    ),
    ("missing", "NAMED", "SELECT @nope AS v", [_param("x", "INT64", "1")]),
]


def _row(**changes: str) -> str:
    values = {
        "id": "1",
        "b": "true",
        "ts": _TS,
        "d": "2024-01-02",
        "n": "1.5",
        "f": "1.5",
        "s": "x",
    }
    values.update(changes)
    return ",".join(values[k] for k in ("id", "b", "ts", "d", "n", "f", "s")) + "\n"


LOAD_CASES: dict[str, str] = {
    "baseline": _HEADER
    + _row(s='"quoted, with comma"')
    + _row(
        id="2", b="false", ts="2024-01-02 03:04:05.123456", n="-0.000000001", f="-1.5", s="plain"
    ),
    "empty_fields": _HEADER + _row(b="", ts="", d="", n="", f="", s="") + _row(id="2", s='""'),
    "bool_t": _HEADER + _row(b="T"),
    "bool_1": _HEADER + _row(b="1"),
    "bool_yes": _HEADER + _row(b="yes"),
    "bool_mixed_case": _HEADER + _row(b="False"),
    "ts_t_z": _HEADER + _row(ts="2024-01-02T03:04:05Z"),
    "ts_utc_suffix": _HEADER + _row(ts="2024-01-02 03:04:05.123456 UTC"),
    "ts_offset": _HEADER + _row(ts="2024-01-02 03:04:05+01:00"),
    "ts_epoch": _HEADER + _row(ts="1704164645"),
    "ts_date_only": _HEADER + _row(ts="2024-01-02"),
    "ts_slashes": _HEADER + _row(ts="2024/01/02 03:04:05"),
    "date_one_digit": _HEADER + _row(d="2024-1-2"),
    "date_slashes": _HEADER + _row(d="2024/01/02"),
    "numeric_exponent": _HEADER + _row(n="1e3"),
    "float_inf_nan": _HEADER + _row(f="-inf") + _row(id="2", f="NaN"),
    "bad_value": _HEADER + _row(b="maybe"),
    "bool_upper": _HEADER + _row(b="TRUE"),
    "bool_title_true": _HEADER + _row(b="True"),
    "int_negative": _HEADER + _row(id="-1"),
    "int_plus": _HEADER + _row(id="+1"),
    "int_quoted": _HEADER + _row(id='"1"'),
    "ts_t_no_z": _HEADER + _row(ts="2024-01-02T03:04:05"),
    "ts_fraction_3": _HEADER + _row(ts="2024-01-02 03:04:05.123"),
    "date_invalid": _HEADER + _row(d="2024-02-30"),
    "numeric_many_digits": _HEADER + _row(n="1.0000000001"),
    "string_escaped_quote": _HEADER + _row(s='"a""b"'),
    "crlf": (_HEADER + _row()).replace("\n", "\r\n"),
}


def record_api(
    session: Session,
    project: str,
    max_bytes_billed: int,
    out_dir: Path,
    log: Callable[[str], None],
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(session, project, max_bytes_billed, out_dir)
    recorder.run()
    log(f"wrote {len(recorder.written)} fixtures to {out_dir}")
    return recorder.written
