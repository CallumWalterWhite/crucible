"""BigQuery REST v2 server: the API subset in docs/architecture.md.

Response shapes follow the recorded fixtures in `tests/fixtures/bigquery_api/` (cited by number);
tests/unit/test_bigquery_fixtures.py replays the recording against this server and lists every
remaining difference with its reason. Any bearer token is accepted and never read or logged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlglot import exp
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from crucible.dialects.bigquery import ddl, metadata, types
from crucible.dialects.bigquery.dialect import DIALECT
from crucible.engine import (
    CrucibleError,
    CrucibleUnsupportedError,
    Engine,
    ErrorKind,
    ExecContext,
    Pipeline,
    TableInfo,
)
from crucible.engine.catalog import qualified
from crucible.server.bigquery import encoding
from crucible.server.bigquery.errors import error_response, http_error, job_error_response
from crucible.server.bigquery.jobs import (
    ANONYMOUS_DATASET,
    DEFAULT_LOCATION,
    Job,
    JobStore,
    Runner,
    fields_of,
    new_job_id,
    new_page_token,
    now_ms,
    obj,
    page,
    page_bounds,
    token_offset,
)

DEFAULT_PORT = 9050
API = "/bigquery/v2/projects/{project}"
UPLOAD = "/upload/bigquery/v2/projects/{project}/jobs"
_CONTENT_RANGE = re.compile(r"^bytes (?:(\d+)-(\d+)|\*)/(\d+|\*)$")
_QUERY_REQUEST_KEYS = frozenset(
    {
        "query",
        "useLegacySql",
        "defaultDataset",
        "maximumBytesBilled",
        "labels",
        "maxResults",
        "timeoutMs",
        "location",
        "jobCreationMode",
        "requestId",
        "formatOptions",
        "useQueryCache",
        "kind",
    }
)
# REST reference, Dataset resource: fields Crucible stores and echoes.
_DATASET_FIELDS = ("defaultTableExpirationMs", "labels", "description", "location")
# tabledata.list query parameters the client sends that do not change the rows it reads: paging,
# and the int64 timestamp format it always asks for (google-cloud-bigquery==3.46.1:
# google/cloud/bigquery/client.py:4258 startIndex, :4264 useInt64Timestamp; api-core paging:
# google/api_core/page_iterator.py:397-419). Any other parameter is refused.
_TABLEDATA_PARAMS = frozenset(
    {"maxResults", "pageToken", "startIndex", "formatOptions.useInt64Timestamp"}
)
# Standard Google API query parameters that change nothing in the response
# (https://cloud.google.com/apis/docs/system-parameters).
_SYSTEM_PARAMS = frozenset(
    {
        "alt",
        "callback",
        "key",
        "access_token",
        "oauth_token",
        "prettyPrint",
        "quotaUser",
        "userIp",
        "$.xgafv",
        "upload_protocol",
    }
)

Handler = Callable[[Request], Awaitable[Response]]


@dataclass
class Upload:
    job: Job
    data: bytearray


def _etag() -> str:
    return uuid.uuid4().hex[:16]


def _int_param(request: Request, name: str) -> int | None:
    value = request.query_params.get(name)
    return None if value is None else int(value)


class BigQueryServer:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.store = JobStore()
        self.runner = Runner(engine, self.store)
        self.pipeline = Pipeline(engine, DIALECT)
        self.uploads: dict[str, Upload] = {}

    def routes(self) -> list[BaseRoute]:
        def route(path: str, handler: Handler, method: str) -> Route:
            return Route(path, handler, methods=[method])

        return [
            route(f"{API}/jobs", self.jobs_insert, "POST"),
            route(f"{API}/jobs/{{job}}", self.jobs_get, "GET"),
            route(f"{API}/jobs/{{job}}/cancel", self.jobs_cancel, "POST"),
            route(f"{API}/queries", self.jobs_query, "POST"),
            route(f"{API}/queries/{{job}}", self.get_query_results, "GET"),
            route(f"{API}/datasets", self.datasets_list, "GET"),
            route(f"{API}/datasets", self.datasets_insert, "POST"),
            route(f"{API}/datasets/{{dataset}}", self.datasets_get, "GET"),
            route(f"{API}/datasets/{{dataset}}", self.datasets_delete, "DELETE"),
            route(f"{API}/datasets/{{dataset}}/tables", self.tables_list, "GET"),
            route(f"{API}/datasets/{{dataset}}/tables", self.tables_insert, "POST"),
            route(f"{API}/datasets/{{dataset}}/tables/{{table}}", self.tables_get, "GET"),
            route(f"{API}/datasets/{{dataset}}/tables/{{table}}", self.tables_patch, "PATCH"),
            route(f"{API}/datasets/{{dataset}}/tables/{{table}}", self.tables_delete, "DELETE"),
            route(f"{API}/datasets/{{dataset}}/tables/{{table}}/data", self.tabledata_list, "GET"),
            route(f"{API}/datasets/{{dataset}}/routines", self.routines_list, "GET"),
            route(f"{API}/datasets/{{dataset}}/routines/{{routine}}", self.routines_get, "GET"),
            route(UPLOAD, self.upload_initiate, "POST"),
            route(UPLOAD, self.upload_chunk, "PUT"),
        ]

    def reset(self) -> None:
        """Drop jobs and uploads; the engine is reset by the caller."""
        self.store.clear()
        self.uploads.clear()

    async def not_implemented(self, request: Request) -> JSONResponse:
        return error_response(
            501,
            "notImplemented",
            f"Not implemented in Crucible: {request.method} {request.url.path}",
            "UNIMPLEMENTED",
        )

    async def internal_error(self, request: Request, exc: Exception) -> JSONResponse:
        return http_error(CrucibleError(ErrorKind.INTERNAL, f"Crucible internal error: {exc}"))

    # --- helpers ------------------------------------------------------------------------------
    def _context_run(self, project: str, work: Callable[[ExecContext], Any]) -> Any:
        session = DIALECT.new_session(project)
        with self.engine.cursor(session) as cursor:
            return work(ExecContext(self.engine, DIALECT, session, cursor, None))

    def _sql(self, project: str, sql: str) -> None:
        self.pipeline.execute(DIALECT.new_session(project), sql)

    def _base(self, request: Request) -> str:
        return str(request.base_url).rstrip("/")

    # --- jobs ---------------------------------------------------------------------------------
    def job_resource(self, job: Job, base: str) -> dict[str, Any]:
        """jobs.insert / jobs.get / upload response (fixtures jobs_get_ctas, jobs_get_select,
        jobs_get_dml, load_baseline_jobs_get, jobs_insert_runtime_error)."""
        configuration: dict[str, Any] = {
            k: (dict(v) if isinstance(v, dict) else v)  # pyright: ignore[reportUnknownArgumentType]
            for k, v in job.configuration.items()
        }
        statistics: dict[str, Any] = {
            "creationTime": str(job.created_ms),
            "startTime": str(job.created_ms),
        }
        if job.job_type == "QUERY":
            query: dict[str, Any] = configuration["query"]
            if job.anonymous_table is not None:
                query["destinationTable"] = {
                    "projectId": job.project,
                    "datasetId": ANONYMOUS_DATASET,
                    "tableId": job.anonymous_table,
                }
                query["writeDisposition"] = "WRITE_TRUNCATE"
            elif job.target is not None:
                project, dataset, table = job.target
                query["destinationTable"] = {
                    "projectId": project,
                    "datasetId": dataset,
                    "tableId": table,
                }
            query.setdefault("priority", "INTERACTIVE")
            if job.done:
                statistics["query"] = self._query_statistics(job)
                if job.statement_type is not None:
                    statistics["totalBytesProcessed"] = "0"
        elif job.load_statistics is not None:
            statistics["load"] = job.load_statistics
        configuration["jobType"] = job.job_type
        if job.done:
            statistics["endTime"] = str(job.ended_ms)
        status: dict[str, Any] = {"state": "DONE" if job.done else "RUNNING"}
        if job.error is not None:
            status = {"errorResult": job.error, "errors": job.errors, **status}
        return {
            "kind": "bigquery#job",
            "etag": _etag(),
            "id": f"{job.project}:{job.location}.{job.job_id}",
            "selfLink": f"{base}/bigquery/v2/projects/{job.project}/jobs/{job.job_id}"
            f"?location={job.location}",
            "configuration": configuration,
            "jobReference": self._reference(job),
            "statistics": statistics,
            "status": status,
            "jobCreationReason": {"code": "REQUESTED"},
        }

    def _query_statistics(self, job: Job) -> dict[str, Any]:
        if job.statement_type is None:
            return {}  # failed before its statement type was known (load_bad_value_select_insert)
        stats: dict[str, Any] = {
            "totalBytesProcessed": "0",
            "totalBytesBilled": "0",
            "cacheHit": False,
            "statementType": job.statement_type,
        }
        if job.dml_affected_rows is not None:
            stats["numDmlAffectedRows"] = str(job.dml_affected_rows)
            if job.statement_type == "INSERT":
                stats["dmlStats"] = {"insertedRowCount": str(job.dml_affected_rows)}
        if job.statement_type == "CREATE_TABLE_AS_SELECT" and job.target is not None:
            project, dataset, table = job.target
            if job.target_schema is not None:
                stats["schema"] = job.target_schema
            stats["ddlTargetTable"] = {
                "projectId": project,
                "datasetId": dataset,
                "tableId": table,
            }
        return stats

    def _reference(self, job: Job) -> dict[str, str]:
        return {"projectId": job.project, "jobId": job.job_id, "location": job.location}

    async def jobs_insert(self, request: Request) -> Response:
        project = request.path_params["project"]
        body: dict[str, Any] = await request.json()
        reference = obj(body.get("jobReference"))
        configuration: dict[str, Any] = dict(obj(body.get("configuration")))
        try:
            kinds = [k for k in ("query", "load", "copy", "extract") if k in configuration]
            if kinds != ["query"]:
                raise CrucibleUnsupportedError(
                    f"jobs.insert of a {kinds[0] if kinds else 'empty'} job (only query jobs; "
                    "load jobs use the resumable upload)"
                )
            if configuration.get("dryRun"):
                raise CrucibleUnsupportedError("dry-run jobs")
            job = Job(
                project=project,
                job_id=reference.get("jobId") or new_job_id(),
                location=reference.get("location") or DEFAULT_LOCATION,
                job_type="QUERY",
                configuration=configuration,
            )
            self.store.add(job)
        except CrucibleError as exc:
            return http_error(exc)
        await run_in_threadpool(self.runner.run_query, job)
        return JSONResponse(self.job_resource(job, self._base(request)))

    async def jobs_get(self, request: Request) -> Response:
        try:
            job = self.store.get(request.path_params["project"], request.path_params["job"])
        except CrucibleError as exc:
            return http_error(exc)
        return JSONResponse(self.job_resource(job, self._base(request)))

    async def jobs_cancel(self, request: Request) -> Response:
        # Jobs finish inside the request that creates them, so there is nothing to cancel.
        # Response shape: REST reference, jobs.cancel (JobCancelResponse).
        try:
            job = self.store.get(request.path_params["project"], request.path_params["job"])
        except CrucibleError as exc:
            return http_error(exc)
        return JSONResponse(
            {
                "kind": "bigquery#jobCancelResponse",
                "job": self.job_resource(job, self._base(request)),
            }
        )

    def _rows_response(
        self,
        job: Job,
        *,
        max_results: int | None,
        page_token: str | None,
        start_index: int | None,
        int64_timestamp: bool,
    ) -> dict[str, Any]:
        """The result fields shared by jobs.query (jobs_query) and getQueryResults
        (get_query_results*)."""
        response: dict[str, Any] = {}
        if job.columns is None:
            # Not a SELECT: no totalRows, so the client returns an empty result
            # (google-cloud-bigquery==3.45.2: google/cloud/bigquery/job/query.py:1827).
            if job.dml_affected_rows is not None:
                response["numDmlAffectedRows"] = str(job.dml_affected_rows)
            return response
        columns = [(c.name, c.type) for c in job.columns]
        rows, token = page(
            job, max_results=max_results, page_token=page_token, start_index=start_index
        )
        response["schema"] = encoding.schema(columns)
        response["totalRows"] = str(len(job.rows))
        if token is not None:
            response["pageToken"] = token
        if rows:
            response["rows"] = encoding.encode_rows(
                rows, [t for _, t in columns], int64_timestamp=int64_timestamp
            )
        return response

    async def get_query_results(self, request: Request) -> Response:
        params = request.query_params
        try:
            job = self.store.get(request.path_params["project"], request.path_params["job"])
            if job.error is not None:
                assert job.error_kind is not None
                return job_error_response(job.error, job.error_kind)
            rows = self._rows_response(
                job,
                max_results=_int_param(request, "maxResults"),
                page_token=params.get("pageToken"),
                start_index=_int_param(request, "startIndex"),
                # requests sends the client's Python True as "True" (google-cloud-bigquery==3.46.1:
                # google/cloud/bigquery/client.py:2144)
                int64_timestamp=params.get("formatOptions.useInt64Timestamp", "").lower() == "true",
            )
        except CrucibleError as exc:
            return http_error(exc, query_parameter=True)
        schema = {"schema": rows.pop("schema")} if "schema" in rows else {}
        return JSONResponse(
            {
                "kind": "bigquery#getQueryResultsResponse",
                "etag": _etag(),
                **schema,
                "jobReference": self._reference(job),
                **rows,
                "totalBytesProcessed": "0",
                "jobComplete": True,
                "cacheHit": False,
            }
        )

    async def jobs_query(self, request: Request) -> Response:
        project = request.path_params["project"]
        body: dict[str, Any] = await request.json()
        try:
            for key in body:
                if key not in _QUERY_REQUEST_KEYS:
                    raise CrucibleUnsupportedError(f"jobs.query request field {key}")
            config = {
                k: body[k]
                for k in ("query", "useLegacySql", "defaultDataset", "maximumBytesBilled", "labels")
                if k in body
            }
            job = Job(
                project=project,
                job_id=new_job_id(),
                location=body.get("location") or DEFAULT_LOCATION,
                job_type="QUERY",
                configuration={"query": config},
            )
            self.store.add(job)
        except CrucibleError as exc:
            return http_error(exc, query_parameter=True)
        await run_in_threadpool(self.runner.run_query, job)
        if job.error is not None:
            assert job.error_kind is not None
            return job_error_response(job.error, job.error_kind)
        try:
            rows = self._rows_response(
                job,
                max_results=body.get("maxResults"),
                page_token=None,
                start_index=None,
                int64_timestamp=bool(obj(body.get("formatOptions")).get("useInt64Timestamp")),
            )
        except CrucibleError as exc:
            return http_error(exc, query_parameter=True)
        schema = {"schema": rows.pop("schema")} if "schema" in rows else {}
        # JOB_CREATION_OPTIONAL: no jobReference while the whole result fits in this response
        # (jobs_query_job_creation_optional); a paged result needs a job, which the client reads
        # back by its reference
        # (google-cloud-bigquery==3.45.2: google/cloud/bigquery/_job_helpers.py:591).
        with_job = body.get("jobCreationMode") != "JOB_CREATION_OPTIONAL" or "pageToken" in rows
        reference: dict[str, Any] = {"jobReference": self._reference(job)} if with_job else {}
        response: dict[str, Any] = {
            "kind": "bigquery#queryResponse",
            **schema,
            **reference,
            **rows,
            "totalBytesProcessed": "0",
            "jobComplete": True,
            "cacheHit": False,
            "queryId": job.job_id,
        }
        if with_job:
            # REST reference, JobCreationReason.Code: REQUESTED, or LARGE_RESULTS when a job
            # was created for a JOB_CREATION_OPTIONAL query.
            optional = body.get("jobCreationMode") == "JOB_CREATION_OPTIONAL"
            response["jobCreationReason"] = {"code": "LARGE_RESULTS" if optional else "REQUESTED"}
        response |= {
            "totalBytesBilled": "0",
            "location": job.location,
            "creationTime": str(job.created_ms),
            "startTime": str(job.created_ms),
            "endTime": str(job.ended_ms),
        }
        if with_job and job.columns is not None:
            response["pageRowCount"] = str(len(rows.get("rows", [])))
        if job.statement_type is not None:
            response["statementType"] = job.statement_type
        return JSONResponse(response)

    # --- datasets -----------------------------------------------------------------------------
    def _dataset_props(self, ctx: ExecContext, project: str, dataset: str) -> dict[str, Any]:
        metadata.check_dataset(ctx, project, dataset)
        stored: dict[str, Any] = ctx.catalog.properties(project, dataset, "").get("rest") or {}
        return stored

    def _dataset_resource(
        self, project: str, dataset: str, props: dict[str, Any], base: str, *, full: bool
    ) -> dict[str, Any]:
        """datasets.insert / datasets.get (datasets_insert, datasets_get); the datasets.list
        entry when not full (datasets_list)."""
        resource: dict[str, Any] = {
            "kind": "bigquery#dataset",
            "id": f"{project}:{dataset}",
            "datasetReference": {"datasetId": dataset, "projectId": project},
        }
        extra = {k: props[k] for k in _DATASET_FIELDS if k in props and k != "location"}
        if full:
            resource = {
                "kind": "bigquery#dataset",
                "etag": _etag(),
                "id": resource["id"],
                "selfLink": f"{base}/bigquery/v2/projects/{project}/datasets/{dataset}",
                "datasetReference": resource["datasetReference"],
                **extra,
            }
            for key in ("creationTime", "lastModifiedTime"):
                if key in props:
                    resource[key] = props[key]
        elif "labels" in props:
            resource["labels"] = props["labels"]
        return resource | {"location": props.get("location", DEFAULT_LOCATION), "type": "DEFAULT"}

    async def datasets_insert(self, request: Request) -> Response:
        project = request.path_params["project"]
        body: dict[str, Any] = await request.json()
        reference = obj(body.get("datasetReference"))
        dataset = str(reference.get("datasetId") or "")

        def create(ctx: ExecContext) -> dict[str, Any]:
            if not dataset:
                raise CrucibleError(ErrorKind.INVALID_QUERY, "Dataset ID must be specified")
            _check_fields(body, (*_DATASET_FIELDS, "datasetReference", "kind"), "datasets.insert")
            props = {k: body[k] for k in _DATASET_FIELDS if k in body}
            stamp = str(now_ms())
            props |= {"creationTime": stamp, "lastModifiedTime": stamp}
            for statement in DIALECT.split(f"CREATE SCHEMA `{project}`.`{dataset}`"):
                ctx.execute(statement)
            ctx.catalog.set_properties(project, dataset, "", {"rest": props})
            return props

        try:
            props = await run_in_threadpool(self._context_run, project, create)
        except CrucibleError as exc:
            return http_error(exc)
        return JSONResponse(
            self._dataset_resource(project, dataset, props, self._base(request), full=True)
        )

    async def datasets_get(self, request: Request) -> Response:
        project, dataset = request.path_params["project"], request.path_params["dataset"]
        try:
            props = await run_in_threadpool(
                self._context_run, project, lambda c: self._dataset_props(c, project, dataset)
            )
        except CrucibleError as exc:
            return http_error(exc)
        return JSONResponse(
            self._dataset_resource(project, dataset, props, self._base(request), full=True)
        )

    async def datasets_list(self, request: Request) -> Response:
        project = request.path_params["project"]

        def listing(ctx: ExecContext) -> list[dict[str, Any]]:
            wanted = _label_filter(request.query_params.get("filter"))
            entries: list[dict[str, Any]] = []
            for dataset in ctx.catalog.schemas(project):
                props = self._dataset_props(ctx, project, dataset)
                labels: dict[str, str] = obj(props.get("labels"))
                if all(k in labels and v in (None, labels[k]) for k, v in wanted.items()):
                    entries.append(self._dataset_resource(project, dataset, props, "", full=False))
            return entries

        try:
            entries = await run_in_threadpool(self._context_run, project, listing)
        except CrucibleError as exc:
            return http_error(exc)
        body: dict[str, Any] = {"kind": "bigquery#datasetList", "etag": _etag()}
        if entries:
            body["datasets"] = entries
        return JSONResponse(body)

    async def datasets_delete(self, request: Request) -> Response:
        project, dataset = request.path_params["project"], request.path_params["dataset"]
        contents = request.query_params.get("deleteContents") == "true"

        def delete(ctx: ExecContext) -> None:
            metadata.check_dataset(ctx, project, dataset)
            if not contents and ctx.catalog.tables(project, dataset):
                raise CrucibleUnsupportedError(
                    "datasets.delete of a dataset that is not empty, without deleteContents"
                )
            ctx.catalog.drop_schema(project, dataset, cascade=True)
            ctx.catalog.clear_properties(project, dataset, "")

        try:
            await run_in_threadpool(self._context_run, project, delete)
        except CrucibleError as exc:
            return http_error(exc)
        return Response(status_code=204)

    # --- tables -------------------------------------------------------------------------------
    def _table_resource(
        self, info: TableInfo, rows: int | None, base: str, location: str
    ) -> dict[str, Any]:
        """tables.get / tables.patch / tables.insert (tables_get, tables_patch_add_column).
        Storage sizes and object times
        are not emulated."""
        project, dataset, table = info.catalog, info.schema, info.name
        resource: dict[str, Any] = {
            "kind": "bigquery#table",
            "etag": _etag(),
            "id": f"{project}:{dataset}.{table}",
            "selfLink": f"{base}/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}",
            "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
            "schema": encoding.schema([(c.name, c.type) for c in info.columns], with_mode=False),
            **ddl.rest_partitioning(info.properties),
        }
        if rows is not None:
            resource["numRows"] = str(rows)
        return resource | {"type": "VIEW" if info.kind == "view" else "TABLE", "location": location}

    def _find(self, ctx: ExecContext, project: str, dataset: str, table: str) -> TableInfo:
        return metadata.find_table(ctx, metadata.Name(project, dataset, table))

    def _describe(self, ctx: ExecContext, info: TableInfo, base: str) -> dict[str, Any]:
        rows = None
        if info.kind == "table":
            count = ctx.run_sql(
                f"select count(*) from {qualified(info.catalog, info.schema, info.name)}"
            )
            rows = int(count.rows[0][0])
        location = self._dataset_props(ctx, info.catalog, info.schema).get(
            "location", DEFAULT_LOCATION
        )
        return self._table_resource(info, rows, base, location)

    async def tables_get(self, request: Request) -> Response:
        p = request.path_params
        project, dataset, table = p["project"], p["dataset"], p["table"]
        base = self._base(request)
        if dataset == ANONYMOUS_DATASET:
            return self._anonymous_table(project, table, base)

        def describe(ctx: ExecContext) -> dict[str, Any]:
            return self._describe(ctx, self._find(ctx, project, dataset, table), base)

        try:
            return JSONResponse(await run_in_threadpool(self._context_run, project, describe))
        except CrucibleError as exc:
            return http_error(exc)

    async def tabledata_list(self, request: Request) -> Response:
        """tabledata.list: a table's rows in storage order, paged like getQueryResults."""
        p = request.path_params
        project, dataset, table = p["project"], p["dataset"], p["table"]
        params = request.query_params
        try:
            _refuse_changed_answers(params)
            max_results = _int_arg(params, "maxResults")
            start_index = _int_arg(params, "startIndex")
            page_token = params.get("pageToken")
            # requests sends the client's Python True as "True" (as getQueryResults does).
            int64 = params.get("formatOptions.useInt64Timestamp", "").lower() == "true"
            if dataset == ANONYMOUS_DATASET:
                job = self.store.anonymous(project, table)
                if job is None or job.columns is None:
                    raise CrucibleError(
                        ErrorKind.NOT_FOUND,
                        f"Not found: Table {project}:{ANONYMOUS_DATASET}.{table}",
                    )
                start, end, token = page_bounds(
                    len(job.rows),
                    max_results=max_results,
                    page_token=page_token,
                    start_index=start_index,
                )
                body = _table_data(
                    len(job.rows),
                    token,
                    job.rows[start:end],
                    [c.type for c in job.columns],
                    int64=int64,
                )
                return JSONResponse(body)

            def read(ctx: ExecContext) -> dict[str, Any]:
                info = self._find(ctx, project, dataset, table)
                if info.kind == "view":
                    raise CrucibleUnsupportedError("tabledata.list of a view")
                name = qualified(info.catalog, info.schema, info.name)
                total = int(ctx.run_sql(f"select count(*) from {name}").rows[0][0])
                start, end, token = page_bounds(
                    total,
                    max_results=max_results,
                    page_token=page_token,
                    start_index=start_index,
                )
                rows: list[tuple[Any, ...]] = []
                if end > start:
                    # rowid is storage order: a table's rows come back as they were inserted.
                    rows = ctx.run_sql(
                        f"select * from {name} order by rowid limit ? offset ?",
                        [end - start, start],
                    ).rows
                return _table_data(total, token, rows, [c.type for c in info.columns], int64=int64)

            return JSONResponse(await run_in_threadpool(self._context_run, project, read))
        except CrucibleError as exc:
            return http_error(exc)

    def _anonymous_table(self, project: str, table: str, base: str) -> Response:
        """A SELECT job's result table, from the job store (tables_get_anonymous: modes are
        NULLABLE here)."""
        job = self.store.anonymous(project, table)
        if job is None or job.columns is None:
            return http_error(
                CrucibleError(
                    ErrorKind.NOT_FOUND, f"Not found: Table {project}:{ANONYMOUS_DATASET}.{table}"
                )
            )
        path = f"{project}/datasets/{ANONYMOUS_DATASET}/tables/{table}"
        return JSONResponse(
            {
                "kind": "bigquery#table",
                "etag": _etag(),
                "id": f"{project}:{ANONYMOUS_DATASET}.{table}",
                "selfLink": f"{base}/bigquery/v2/projects/{path}",
                "tableReference": {
                    "projectId": project,
                    "datasetId": ANONYMOUS_DATASET,
                    "tableId": table,
                },
                "schema": encoding.schema([(c.name, c.type) for c in job.columns]),
                "numRows": str(len(job.rows)),
                "type": "TABLE",
                "location": job.location,
            }
        )

    async def tables_list(self, request: Request) -> Response:
        project, dataset = request.path_params["project"], request.path_params["dataset"]
        max_results = _int_param(request, "maxResults")
        token = request.query_params.get("pageToken") or "0"
        offset = token_offset(token)

        def listing(ctx: ExecContext) -> list[TableInfo]:
            metadata.check_dataset(ctx, project, dataset)
            return ctx.catalog.tables(project, dataset)

        try:
            infos = await run_in_threadpool(self._context_run, project, listing)
        except CrucibleError as exc:
            return http_error(exc)
        end = len(infos) if max_results is None else min(len(infos), offset + max_results)
        body: dict[str, Any] = {"kind": "bigquery#tableList", "etag": _etag()}
        if end < len(infos):
            body["nextPageToken"] = new_page_token(end)
        tables = [
            {
                "kind": "bigquery#table",
                "id": f"{project}:{dataset}.{info.name}",
                "tableReference": {
                    "projectId": project,
                    "datasetId": dataset,
                    "tableId": info.name,
                },
                "type": "VIEW" if info.kind == "view" else "TABLE",
                **ddl.rest_partitioning(info.properties, definition=False),
            }
            for info in infos[offset:end]
        ]
        if tables:
            body["tables"] = tables
        return JSONResponse(body | {"totalItems": len(infos)})

    async def tables_insert(self, request: Request) -> Response:
        project, dataset = request.path_params["project"], request.path_params["dataset"]
        body: dict[str, Any] = await request.json()
        base = self._base(request)

        def create(ctx: ExecContext) -> dict[str, Any]:
            _check_fields(body, ("tableReference", "schema", "kind"), "tables.insert")
            table = obj(body.get("tableReference")).get("tableId")
            if not table:
                raise CrucibleError(ErrorKind.INVALID_QUERY, "Table ID must be specified")
            metadata.check_dataset(ctx, project, dataset)
            if ctx.catalog.table(project, dataset, table) is not None:
                raise CrucibleError(
                    ErrorKind.ALREADY_EXISTS, f"Already Exists: Table {project}:{dataset}.{table}"
                )
            fields: list[dict[str, Any]] = fields_of(obj(body.get("schema")))
            columns = ", ".join(_column_sql(f) for f in fields)
            for statement in DIALECT.split(
                f"CREATE TABLE `{project}`.`{dataset}`.`{table}` ({columns})"
            ):
                ctx.execute(statement)
            return self._describe(ctx, self._find(ctx, project, dataset, table), base)

        try:
            return JSONResponse(await run_in_threadpool(self._context_run, project, create))
        except CrucibleError as exc:
            return http_error(exc)

    async def tables_patch(self, request: Request) -> Response:
        """Appending columns (dbt `on_schema_change: append_new_columns`, 82)."""
        p = request.path_params
        project, dataset, table = p["project"], p["dataset"], p["table"]
        body: dict[str, Any] = await request.json()
        base = self._base(request)

        def patch(ctx: ExecContext) -> dict[str, Any]:
            _check_fields(body, ("schema", "tableReference", "kind"), "tables.patch")
            info = self._find(ctx, project, dataset, table)
            fields: list[dict[str, Any]] = fields_of(obj(body.get("schema")))
            existing = [(c.name, encoding.schema_field(c.name, c.type)) for c in info.columns]
            if "schema" in body:
                if len(fields) < len(existing):
                    raise CrucibleUnsupportedError("tables.patch that removes columns")
                for (name, current), wanted in zip(existing, fields, strict=False):
                    if not _same_field(current, wanted) or name != wanted.get("name"):
                        raise CrucibleUnsupportedError(f"tables.patch that changes column {name}")
                full = f"`{project}`.`{dataset}`.`{table}`"
                for added in fields[len(existing) :]:
                    for statement in DIALECT.split(
                        f"ALTER TABLE {full} ADD COLUMN {_column_sql(added)}"
                    ):
                        ctx.execute(statement)
            return self._describe(ctx, self._find(ctx, project, dataset, table), base)

        try:
            return JSONResponse(await run_in_threadpool(self._context_run, project, patch))
        except CrucibleError as exc:
            return http_error(exc)

    async def tables_delete(self, request: Request) -> Response:
        p = request.path_params
        project, dataset, table = p["project"], p["dataset"], p["table"]

        def delete(ctx: ExecContext) -> None:
            info = self._find(ctx, project, dataset, table)
            kind = "VIEW" if info.kind == "view" else "TABLE"
            for statement in DIALECT.split(f"DROP {kind} `{project}`.`{dataset}`.`{table}`"):
                ctx.execute(statement)

        try:
            await run_in_threadpool(self._context_run, project, delete)
        except CrucibleError as exc:
            return http_error(exc)
        return Response(status_code=204)

    # --- routines (none are emulated) ---------------------------------------------------------
    async def routines_list(self, request: Request) -> Response:
        project, dataset = request.path_params["project"], request.path_params["dataset"]
        try:
            await run_in_threadpool(
                self._context_run, project, lambda c: metadata.check_dataset(c, project, dataset)
            )
        except CrucibleError as exc:
            return http_error(exc)
        return JSONResponse({})  # an empty dataset lists as {} (routines_list)

    async def routines_get(self, request: Request) -> Response:
        p = request.path_params
        return http_error(
            CrucibleError(
                ErrorKind.NOT_FOUND,
                f"Not found: Routine {p['project']}:{p['dataset']}.{p['routine']}",
            )
        )

    # --- resumable upload (load jobs; docs/client-compat.md, "Resumable load upload") ---------
    async def upload_initiate(self, request: Request) -> Response:
        project = request.path_params["project"]
        if request.query_params.get("uploadType") != "resumable":
            return http_error(
                CrucibleUnsupportedError(
                    f"uploadType={request.query_params.get('uploadType')} (only resumable)"
                )
            )
        body: dict[str, Any] = await request.json()
        reference = obj(body.get("jobReference"))
        configuration: dict[str, Any] = dict(obj(body.get("configuration")))
        try:
            if list(configuration) != ["load"] and set(configuration) - {"load", "labels"}:
                raise CrucibleUnsupportedError("an upload whose job is not a load job")
            if "load" not in configuration:
                raise CrucibleError(ErrorKind.INVALID_QUERY, "Upload job needs configuration.load")
            job = Job(
                project=project,
                job_id=reference.get("jobId") or new_job_id(),
                location=reference.get("location") or DEFAULT_LOCATION,
                job_type="LOAD",
                configuration=configuration,
            )
            self.store.add(job)
        except CrucibleError as exc:
            return http_error(exc)
        upload_id = uuid.uuid4().hex
        self.uploads[upload_id] = Upload(job, bytearray())
        location = (
            f"{self._base(request)}/upload/bigquery/v2/projects/{project}/jobs"
            f"?uploadType=resumable&upload_id={upload_id}"
        )
        return Response(status_code=200, headers={"Location": location})  # 21

    async def upload_chunk(self, request: Request) -> Response:
        """One PUT of the resumable protocol: 200 with the job when the upload is complete,
        else 308 with the received Range (google-resumable-media==2.11.0:
        google/resumable_media/_upload.py:686-722)."""
        upload = self.uploads.get(request.query_params.get("upload_id") or "")
        if upload is None:
            return error_response(404, "notFound", "Not found: Upload", "NOT_FOUND")
        match = _CONTENT_RANGE.match(request.headers.get("content-range", ""))
        if match is None:
            return error_response(
                400, "invalid", "Crucible needs a Content-Range header", "INVALID_ARGUMENT"
            )
        start, end, total = match.groups()
        chunk = await request.body()
        if start is not None:
            if int(start) != len(upload.data) or int(end) - int(start) + 1 != len(chunk):
                return error_response(
                    400,
                    "invalid",
                    "Content-Range does not match the upload so far and this chunk",
                    "INVALID_ARGUMENT",
                )
            upload.data.extend(chunk)
        if total == "*" or int(total) != len(upload.data):
            headers = {"Range": f"bytes=0-{len(upload.data) - 1}"} if upload.data else {}
            return Response(status_code=308, headers=headers)
        upload_id = request.query_params["upload_id"]
        self.uploads.pop(upload_id, None)
        await run_in_threadpool(self.runner.run_load, upload.job, bytes(upload.data))
        return JSONResponse(self.job_resource(upload.job, self._base(request)))


def _check_fields(body: dict[str, Any], allowed: tuple[str, ...], call: str) -> None:
    """Refuse request fields Crucible would ignore. Empty ones (the client sends `labels: {}`)
    change nothing."""
    for key, value in body.items():
        if key not in allowed and value not in (None, {}, []):
            raise CrucibleUnsupportedError(f"{call} field {key}")


def _refuse_changed_answers(params: Any) -> None:
    """Refuse query parameters that would change the rows: a column subset, a timestamp format
    other than the int64 one, and anything unknown."""
    for key, value in params.items():
        if key == "selectedFields":
            if value:
                raise CrucibleUnsupportedError("tabledata.list with selectedFields")
        elif key not in _TABLEDATA_PARAMS and key not in _SYSTEM_PARAMS:
            raise CrucibleUnsupportedError(f"tabledata.list query parameter {key}")


def _int_arg(params: Any, name: str) -> int | None:
    value = params.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise CrucibleError(ErrorKind.INVALID_QUERY, f"Invalid {name}: {value!r}") from exc


def _table_data(
    total: int,
    token: str | None,
    rows: list[tuple[Any, ...]],
    column_types: list[exp.DataType],
    *,
    int64: bool,
) -> dict[str, Any]:
    """A TableDataList (REST reference, tabledata/list, response body): kind, etag, totalRows,
    pageToken and rows. The client reads `rows` and `pageToken`
    (google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1953 items_key, :1958
    next_token) and `totalRows` (table.py:4139). `pageToken` is present only while rows remain
    and `rows` only when the page has some."""
    # REST reference, tabledata/list: TableDataList.kind and .etag.
    body: dict[str, Any] = {"kind": "bigquery#tableDataList", "etag": _etag()}
    # REST reference: totalRows is int64, so a string. Read by google-cloud-bigquery==3.46.1
    # google/cloud/bigquery/table.py:4139 (_rows_page_start).
    body["totalRows"] = str(total)
    if token is not None:
        # google-api-core==2.42.0: google/api_core/page_iterator.py:377 (next token)
        body["pageToken"] = token
    if rows:
        # google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1953 (items key "rows")
        body["rows"] = encoding.encode_rows(
            rows, column_types, int64_timestamp=int64, tabledata=True
        )
    return body


def _column_sql(field: dict[str, Any]) -> str:
    if field.get("mode", "NULLABLE").upper() == "REQUIRED":
        raise CrucibleUnsupportedError("a REQUIRED column in a REST schema")
    if field.get("description") or field.get("policyTags") or field.get("defaultValueExpression"):
        raise CrucibleUnsupportedError(f"schema field options on column {field.get('name')}")
    data_type = encoding.warehouse_type_from_field(field)
    name = str(field["name"]).replace("`", "")
    return f"`{name}` {types.type_name(data_type)}"


def _same_field(current: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """A patched field equals the stored one (legacy or standard type names, NULLABLE default)."""
    try:
        return types.type_name(encoding.warehouse_type_from_field(current)) == types.type_name(
            encoding.warehouse_type_from_field(wanted)
        )
    except CrucibleError:
        return False


def _label_filter(text: str | None) -> dict[str, str | None]:
    """datasets.list `filter`: space-separated `labels.<key>[:<value>]` terms (REST reference,
    datasets.list); anything else is unsupported."""
    wanted: dict[str, str | None] = {}
    for term in (text or "").split():
        if not term.startswith("labels."):
            raise CrucibleUnsupportedError(f"datasets.list filter {term}")
        key, _, value = term.removeprefix("labels.").partition(":")
        wanted[key] = value or None
    return wanted
