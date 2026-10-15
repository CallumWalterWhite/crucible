"""CrucibleError -> BigQuery error bodies, as recorded (`tests/fixtures/bigquery_api/`).

- HTTP error body: {"error": {code, message, errors: [{message, domain, reason, ...}], status}}
  (87_tables_get_not_found.json; google-api-core==2.42.0: google/api_core/exceptions.py:551-552).
- A query error from jobs.query or getQueryResults is HTTP 400, reason invalidQuery, with
  location "q" / locationType "parameter" (85_get_query_results_runtime_error.json,
  86_jobs_query_syntax_error.json).
- A failed job is HTTP 200 with status.errorResult and status.errors: invalidQuery with location
  "query" (83_jobs_insert_runtime_error.json); notFound without a location, and the message names
  the location (29_load_empty_fields_select_insert.json).
- Duplicates are HTTP 409, reason duplicate, status ALREADY_EXISTS (88, 89).
"""

from __future__ import annotations

import re
from typing import Any

from starlette.responses import JSONResponse

from crucible.engine import CrucibleError, ErrorKind

_HTTP: dict[ErrorKind, tuple[int, str, str]] = {
    ErrorKind.INVALID_QUERY: (400, "invalidQuery", "INVALID_ARGUMENT"),
    ErrorKind.RUNTIME: (400, "invalidQuery", "INVALID_ARGUMENT"),
    ErrorKind.UNSUPPORTED: (400, "invalidQuery", "INVALID_ARGUMENT"),
    ErrorKind.CONFLICT: (400, "invalidQuery", "INVALID_ARGUMENT"),
    ErrorKind.NOT_FOUND: (404, "notFound", "NOT_FOUND"),
    ErrorKind.ALREADY_EXISTS: (409, "duplicate", "ALREADY_EXISTS"),
    # Not a reason the client retries (google-cloud-bigquery==3.45.2: google/cloud/bigquery/
    # retry.py:25-26), so a Crucible bug fails fast instead of being retried for 10 minutes.
    ErrorKind.INTERNAL: (500, "crucibleInternalError", "INTERNAL"),
}
_NOT_FOUND_NAME = re.compile(r"^Not found: (Table|Dataset) \S+$")


def error_response(
    status: int, reason: str, message: str, status_text: str, **detail: str
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason, **detail}],
                "status": status_text,
            }
        },
        status_code=status,
    )


def http_error(exc: CrucibleError, *, query_parameter: bool = False) -> JSONResponse:
    """An HTTP error. query_parameter: the error is about the request's SQL (jobs.query and
    getQueryResults point at parameter "q")."""
    status, reason, status_text = _HTTP[exc.kind]
    detail = (
        {"location": "q", "locationType": "parameter"}
        if query_parameter and reason == "invalidQuery"
        else {}
    )
    return error_response(status, reason, str(exc), status_text, **detail)


def job_error(exc: CrucibleError, location: str) -> dict[str, Any]:
    """A failed job's errorResult (also its only status.errors entry)."""
    _, reason, _ = _HTTP[exc.kind]
    message = str(exc)
    if exc.kind is ErrorKind.NOT_FOUND and _NOT_FOUND_NAME.match(message):
        message = f"{message} was not found in location {location}"
    error: dict[str, Any] = {"reason": reason, "message": message}
    if reason == "invalidQuery":
        error = {"reason": reason, "location": "query", "message": message}
    return error


def job_error_response(error: dict[str, Any], kind: ErrorKind) -> JSONResponse:
    """getQueryResults on a failed job: the job's error as an HTTP error (85)."""
    status, reason, status_text = _HTTP[kind]
    detail = {"location": "q", "locationType": "parameter"} if reason == "invalidQuery" else {}
    return error_response(status, reason, error["message"], status_text, **detail)
