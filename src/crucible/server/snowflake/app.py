"""Snowflake connector REST server (login/session, query-request, results). Phase 5."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute

from crucible.engine import Engine

DEFAULT_PORT = 8080
# Crucible's own error code; the connector requires a numeric code (int(code)) and has no
# public catalogue of server codes to reuse.
NOT_IMPLEMENTED_CODE = "990001"
INTERNAL_ERROR_CODE = "990002"


def error_response(code: str, message: str, sql_state: str) -> JSONResponse:
    # The connector treats non-200 statuses as transport errors and drops the body, so errors are
    # 200 + success=false; it reads message, int(code) and data.sqlState
    # (snowflake-connector-python==4.8.0: snowflake/connector/cursor.py:1046,1127-1128,1145;
    #  snowflake/connector/network.py:1183).
    return JSONResponse(
        {"success": False, "code": code, "message": message, "data": {"sqlState": sql_state}}
    )


class SnowflakeServer:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def routes(self) -> list[BaseRoute]:
        return []

    def reset(self) -> None:
        """Drop sessions; the engine is reset by the caller."""

    async def not_implemented(self, request: Request) -> JSONResponse:
        # SQLSTATE 0A000: feature not supported (ISO/IEC 9075 class 0A).
        return error_response(
            NOT_IMPLEMENTED_CODE,
            f"Not implemented in Crucible: {request.method} {request.url.path}",
            "0A000",
        )

    async def internal_error(self, request: Request, exc: Exception) -> JSONResponse:
        # SQLSTATE XX000: internal error (PostgreSQL convention; ISO leaves class XX to vendors).
        return error_response(INTERNAL_ERROR_CODE, f"Crucible internal error: {exc}", "XX000")
