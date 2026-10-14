"""BigQuery REST v2 server. Phase 3 implements only the API subset in docs/architecture.md."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute

from crucible.engine import Engine

DEFAULT_PORT = 9050


def error_response(status: int, reason: str, message: str, status_text: str) -> JSONResponse:
    # Shape read by google-api-core
    # (google-api-core==2.42.0: google/api_core/exceptions.py:551-552);
    # reasons and HTTP codes: https://cloud.google.com/bigquery/docs/error-messages
    return JSONResponse(
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason}],
                "status": status_text,
            }
        },
        status_code=status,
    )


class BigQueryServer:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def routes(self) -> list[BaseRoute]:
        return []

    def reset(self) -> None:
        """Drop sessions and jobs; the engine is reset by the caller."""

    async def not_implemented(self, request: Request) -> JSONResponse:
        return error_response(
            501,
            "notImplemented",
            f"Not implemented in Crucible: {request.method} {request.url.path}",
            "UNIMPLEMENTED",
        )

    async def internal_error(self, request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "internalError", f"Crucible internal error: {exc}", "INTERNAL")
