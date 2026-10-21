"""The job store and synchronous job execution (ADR 0004).

Every job runs to completion inside the request that creates it, so jobs.insert and jobs.query
answer DONE. Results stay in memory with the job: getQueryResults pages over them, and the
anonymous destination table of a SELECT is answered from here (tables.get).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, cast

from sqlglot import exp

from crucible.dialects.bigquery import metadata, types
from crucible.dialects.bigquery.dialect import DIALECT
from crucible.engine import (
    Column,
    CrucibleError,
    CrucibleUnsupportedError,
    Engine,
    ErrorKind,
    ExecContext,
    Pipeline,
    Result,
)
from crucible.engine.catalog import qualified
from crucible.server.bigquery import encoding, errors, load

_LOG = logging.getLogger(__name__)
DEFAULT_LOCATION = (
    "US"  # BigQuery's default; recorded job ids read "<project>:US.<job>" (jobs_get_ctas)
)
# Anonymous result tables live in a dataset whose name starts with "_" and tables named
# "anon...", as BigQuery's do (jobs_insert_select.json, after redaction).
ANONYMOUS_DATASET = "_crucible_anonymous"
PAGE_TOKEN_PREFIX = "crucible-page-"  # noqa: S105 - a page token, not a credential


def new_page_token(offset: int) -> str:
    """An opaque token for the row at `offset`, unique per page as BigQuery's are."""
    return f"{PAGE_TOKEN_PREFIX}{offset}-{uuid.uuid4().hex[:12]}"


def token_offset(token: str) -> int:
    """The offset a new_page_token names; ValueError for anything else."""
    return int(token.removeprefix(PAGE_TOKEN_PREFIX).split("-", 1)[0])


# Query configuration keys that change nothing in an emulator: they are echoed, not applied.
_IGNORED_QUERY_KEYS = frozenset(
    {
        "query",
        "useLegacySql",
        "defaultDataset",
        "maximumBytesBilled",
        "priority",
        "labels",
        "useQueryCache",  # Crucible has no query cache: every answer is a fresh one
    }
)
_IGNORED_LOAD_KEYS = frozenset({"destinationTable", "schema", "sourceFormat", "skipLeadingRows"})


def obj(value: Any) -> dict[str, Any]:
    """A JSON object from a request body, or {} when absent or not an object."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def fields_of(schema: dict[str, Any]) -> list[dict[str, Any]]:
    fields = schema.get("fields")
    return [obj(f) for f in cast("list[Any]", fields)] if isinstance(fields, list) else []


def now_ms() -> int:
    return time.time_ns() // 1_000_000


SCHEMA_IN_RESULTS = ("UPDATE", "DELETE", "TRUNCATE_TABLE")


@dataclass
class Job:
    project: str
    job_id: str
    location: str
    job_type: str  # QUERY | LOAD
    configuration: dict[str, Any]
    created_ms: int = field(default_factory=now_ms)
    ended_ms: int | None = None
    error: dict[str, Any] | None = None  # status.errorResult
    errors: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    error_kind: ErrorKind | None = None
    statement_type: str | None = None
    columns: list[Column] | None = None  # set for jobs whose result is rows
    rows: list[tuple[Any, ...]] = field(default_factory=list[tuple[Any, ...]])
    dml_affected_rows: int | None = None
    target: tuple[str, str, str] | None = None
    target_schema: dict[str, Any] | None = None
    anonymous_table: str | None = None
    load_statistics: dict[str, str] | None = None
    dry_run: bool = False  # planned, not run, and never stored (REST: configuration.dryRun)

    @property
    def done(self) -> bool:
        return self.ended_ms is not None

    def fail(self, exc: CrucibleError) -> None:
        self.error = errors.job_error(exc, self.location)
        self.errors = [self.error]
        self.error_kind = exc.kind


class JobStore:
    """Thread-safe. A job id is reserved before the job runs, so a duplicate is refused at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], Job] = {}
        self._anonymous: dict[tuple[str, str], Job] = {}

    def add(self, job: Job) -> None:
        with self._lock:
            key = (job.project, job.job_id)
            if key in self._jobs:
                raise CrucibleError(
                    ErrorKind.ALREADY_EXISTS,
                    f"Already Exists: Job {job.project}:{self._jobs[key].location}.{job.job_id}",
                )
            self._jobs[key] = job

    def finished(self, pending: Job, completed: Job) -> None:
        """Publish a finished snapshot and its table together, unless reset removed the job."""
        with self._lock:
            key = (pending.project, pending.job_id)
            # Identity also protects a new job that reused the id after reset.
            if self._jobs.get(key) is not pending:
                return
            self._jobs[key] = completed
            if completed.anonymous_table is not None:
                self._anonymous[(completed.project, completed.anonymous_table)] = completed

    def get(self, project: str, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get((project, job_id))
        if job is None:
            raise CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Job {project}:{job_id}")
        return job

    def anonymous(self, project: str, table: str) -> Job | None:
        with self._lock:
            return self._anonymous.get((project, table))

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._anonymous.clear()


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def _unsupported_keys(config: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    for key in config:
        if key not in allowed:
            raise CrucibleUnsupportedError(f"{what} configuration field {key}")


def check_query_config(config: dict[str, Any]) -> None:
    """Raise for configuration Crucible would silently ignore. Query parameters, destination
    tables and dry runs are not emulated yet."""
    _unsupported_keys(config, _IGNORED_QUERY_KEYS, "query job")
    if config.get("useLegacySql") is not False:
        # jobs.insert and jobs.query default to legacy SQL when the field is absent (REST
        # reference, JobConfigurationQuery.useLegacySql); the clients always send false
        # (google-cloud-bigquery==3.45.2: google/cloud/bigquery/_job_helpers.py:288, :330).
        raise CrucibleUnsupportedError("legacy SQL (useLegacySql is not false)")
    if not isinstance(config.get("query"), str):
        raise CrucibleError(ErrorKind.INVALID_QUERY, "Required parameter is missing: query")


class Runner:
    def __init__(self, engine: Engine, store: JobStore) -> None:
        self.engine = engine
        self.store = store
        self.pipeline = Pipeline(engine, DIALECT)

    # --- queries ----------------------------------------------------------------------------
    def run_query(self, job: Job) -> Job:
        """Run a query job to completion; failures become the job's error, as in BigQuery."""
        pending = job
        job = replace(job)  # Readers keep the untouched RUNNING snapshot until publication.
        config = job.configuration["query"]
        try:
            check_query_config(config)
            default = obj(config.get("defaultDataset"))
            session = DIALECT.new_session(
                default.get("projectId") or job.project, default.get("datasetId")
            )
            results = self.pipeline.execute(session, config["query"])
            self._record(job, session, results)
        except CrucibleError as exc:
            job.fail(exc)
        except Exception as exc:  # noqa: BLE001 - a Crucible bug: fail the job, never leave it RUNNING
            _LOG.exception("query job %s failed inside Crucible", job.job_id)
            job.fail(CrucibleError(ErrorKind.INTERNAL, f"Crucible internal error: {exc!r}"))
        job.ended_ms = now_ms()
        self.store.finished(pending, job)
        return job

    def dry_run_query(self, job: Job) -> Job:
        """Plan a query job without running it (configuration.dryRun): its statement type and,
        for a query, its schema; byte counts are not emulated (always "0")."""
        job = replace(job, dry_run=True)
        config = job.configuration["query"]
        try:
            check_query_config(config)
            default = obj(config.get("defaultDataset"))
            session = DIALECT.new_session(
                default.get("projectId") or job.project, default.get("datasetId")
            )
            result = self.pipeline.dry_run(session, config["query"])
            job.statement_type = result.statement_type
            if result.statement_type == "SELECT":
                for column in result.columns:
                    encoding.schema_field(column.name, column.type)  # raises for unmapped types
                job.columns = result.columns
        except CrucibleError as exc:
            job.fail(exc)
        job.ended_ms = now_ms()
        return job

    def _record(self, job: Job, session: Any, results: list[Result]) -> None:
        last = results[-1]
        # shortcut: a multi-statement request reports its last statement's result as a SCRIPT
        # job without child jobs; upgrade with BigQuery scripting (Phase 4).
        job.statement_type = "SCRIPT" if len(results) > 1 else last.statement_type
        if last.statement_type == "SELECT":
            for column in last.columns:
                if any(t.this == exp.DType.INTERVAL for t in column.type.find_all(exp.DataType)):
                    raise CrucibleUnsupportedError("INTERVAL value in a query result")
                encoding.schema_field(column.name, column.type)  # raises for unmapped types
            job.columns = last.columns
            job.rows = last.rows
            job.anonymous_table = f"anon{uuid.uuid4().hex}"
            return
        if len(results) > 1:
            return
        if last.statement_type in ("INSERT", "MERGE", "UPDATE", "DELETE", "TRUNCATE_TABLE"):
            job.dml_affected_rows = last.rowcount
        target = last.info.get("target")
        # The destinationTable BigQuery reports (recorded for each of these statement types).
        if isinstance(target, tuple) and last.statement_type in (
            "INSERT",
            "MERGE",
            "UPDATE",
            "DELETE",
            "TRUNCATE_TABLE",
            "CREATE_TABLE",
            "CREATE_TABLE_AS_SELECT",
            "ALTER_TABLE",
        ):
            project, dataset, table = (str(part) for part in cast("tuple[Any, ...]", target))
            job.target = (project, dataset, table)
            # getQueryResults of UPDATE, DELETE and TRUNCATE carries the table's schema
            # (get_query_results_update / _delete / _truncate); CTAS statistics carry it too.
            if last.statement_type in ("CREATE_TABLE_AS_SELECT", *SCHEMA_IN_RESULTS):
                with self.engine.cursor(session) as cursor:
                    info = self.engine.catalog(cursor).table(project, dataset, table)
                if info is not None:
                    columns = [(c.name, c.type) for c in info.columns]
                    job.target_schema = encoding.schema(columns, with_mode=False)

    # --- loads ------------------------------------------------------------------------------
    def run_load(self, job: Job, data: bytes) -> Job:
        pending = job
        job = replace(job)
        config = job.configuration["load"]
        try:
            self._load(job, config, data)
        except load.LoadError as exc:
            job.error = {"reason": "invalid", "message": exc.messages[0]}
            job.errors = [{"reason": "invalid", "message": m} for m in exc.messages]
            job.error_kind = ErrorKind.INVALID_QUERY
        except CrucibleError as exc:
            job.fail(exc)
        except Exception as exc:  # noqa: BLE001 - a Crucible bug: fail the job, never leave it RUNNING
            _LOG.exception("load job %s failed inside Crucible", job.job_id)
            job.fail(CrucibleError(ErrorKind.INTERNAL, f"Crucible internal error: {exc!r}"))
        job.ended_ms = now_ms()
        self.store.finished(pending, job)
        return job

    def _load(self, job: Job, config: dict[str, Any], data: bytes) -> None:
        _unsupported_keys(
            config,
            _IGNORED_LOAD_KEYS | {"writeDisposition", "createDisposition", "fieldDelimiter"},
            "load job",
        )
        if config.get("sourceFormat", "CSV") != "CSV":
            raise CrucibleUnsupportedError(f"load job sourceFormat {config.get('sourceFormat')}")
        if config.get("fieldDelimiter", ",") != ",":
            raise CrucibleUnsupportedError("load job fieldDelimiter other than ','")
        if config.get("createDisposition", "CREATE_IF_NEEDED") != "CREATE_IF_NEEDED":
            raise CrucibleUnsupportedError(f"createDisposition {config['createDisposition']}")
        disposition = config.get("writeDisposition", "WRITE_APPEND")
        if disposition not in ("WRITE_TRUNCATE", "WRITE_APPEND"):
            raise CrucibleUnsupportedError(f"writeDisposition {disposition}")
        fields: list[dict[str, Any]] = fields_of(obj(config.get("schema")))
        if not fields:
            raise CrucibleUnsupportedError("a load job without an explicit schema (autodetect)")
        destination = obj(config.get("destinationTable"))
        project = destination.get("projectId") or job.project
        dataset, table = destination.get("datasetId"), destination.get("tableId")
        if not dataset or not table:
            raise CrucibleError(ErrorKind.INVALID_QUERY, "Load job needs a destinationTable")
        rows = load.parse_csv(data, fields, int(config.get("skipLeadingRows", 0)))

        bq_types = [encoding.warehouse_type_from_field(f) for f in fields]
        storage = ", ".join(
            f"{qualified(f['name'])} {types.storage_type(t).sql('duckdb')}"
            for f, t in zip(fields, bq_types, strict=True)
        )
        session = DIALECT.new_session(project, dataset)
        full = qualified(project, dataset, table)
        # A DuckDB transaction writes one attached database, so the rows go to a temp table first
        # and then reach the target in one statement: a failed load leaves the target untouched.
        staging = qualified(f"crucible_load_{uuid.uuid4().hex}")
        with self.engine.cursor(session) as cursor:
            ctx = ExecContext(self.engine, DIALECT, session, cursor, None)
            name = metadata.Name(project, dataset, table)
            metadata.check_new_table(ctx, name)
            info = ctx.catalog.table(project, dataset, table)
            if info is not None and disposition == "WRITE_APPEND":
                stored = [(c.name, types.type_name(c.type)) for c in info.columns]
                wanted = [
                    (f["name"], types.type_name(t)) for f, t in zip(fields, bq_types, strict=True)
                ]
                if stored != wanted:
                    raise CrucibleUnsupportedError(
                        "WRITE_APPEND with a schema that differs from the table's"
                    )
            try:
                ctx.run_sql(f"CREATE TEMP TABLE {staging} ({storage})", returns_rows=False)
                if rows:
                    marks = ", ".join("?" for _ in fields)
                    cursor.executemany(
                        f"INSERT INTO {staging} VALUES ({marks})", [list(r) for r in rows]
                    )
                if info is None or disposition == "WRITE_TRUNCATE":
                    ctx.run_sql(
                        f"CREATE OR REPLACE TABLE {full} AS SELECT * FROM {staging}",
                        returns_rows=False,
                    )
                    ctx.catalog.set_properties(project, dataset, table, {})
                else:
                    ctx.run_sql(f"INSERT INTO {full} SELECT * FROM {staging}", returns_rows=False)
            finally:
                cursor.execute(f"DROP TABLE IF EXISTS {staging}")
        job.load_statistics = {
            "inputFiles": "1",
            "inputFileBytes": str(len(data)),
            "outputRows": str(len(rows)),
            "badRecords": "0",
        }


def page_bounds(
    total: int, *, max_results: int | None, page_token: str | None, start_index: int | None
) -> tuple[int, int, str | None]:
    """Where one page starts and ends among `total` rows, and the next page token if rows remain.

    Shared by getQueryResults (get_query_results*) and tabledata.list (tabledata_list*). A page
    token names the offset of the next row (opaque to clients); it takes precedence over
    startIndex, which a client sends again on every page (api-core==2.42.0:
    google/api_core/page_iterator.py:397-419). maxResults=0 returns no rows and a token when rows
    remain (get_query_results_wait.json); startIndex pages carry a token only if rows remain
    (get_query_results_start_index).
    """
    if page_token is not None:
        try:
            start = token_offset(page_token)
        except ValueError as exc:
            raise CrucibleError(ErrorKind.INVALID_QUERY, "Invalid page token") from exc
    else:
        start = start_index or 0
    if start < 0:
        raise CrucibleError(ErrorKind.INVALID_QUERY, "Invalid startIndex: must not be negative")
    if max_results is not None and max_results < 0:
        raise CrucibleError(ErrorKind.INVALID_QUERY, "Invalid maxResults: must not be negative")
    start = min(start, total)
    end = total if max_results is None else min(total, start + max_results)
    token = new_page_token(end) if end < total else None
    return start, end, token


def page(
    job: Job, *, max_results: int | None, page_token: str | None, start_index: int | None
) -> tuple[list[tuple[Any, ...]], str | None]:
    """The rows of one getQueryResults page and the next page token, if rows remain."""
    start, end, token = page_bounds(
        len(job.rows), max_results=max_results, page_token=page_token, start_index=start_index
    )
    return job.rows[start:end], token
