"""Starlette app factory (ADR 0007). One dialect per process (ADR 0001).

Admin routes live under /_crucible/. Every other path belongs to the dialect server, and any
path it does not implement gets that warehouse's own error shape.
"""

from __future__ import annotations

from typing import Protocol

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from crucible.engine import Engine
from crucible.server.bigquery.app import BigQueryServer
from crucible.server.snowflake.app import SnowflakeServer

DIALECTS = ("bigquery", "snowflake")


class DialectServer(Protocol):
    def routes(self) -> list[BaseRoute]: ...
    def reset(self) -> None: ...
    async def not_implemented(self, request: Request) -> Response: ...
    async def internal_error(self, request: Request, exc: Exception) -> Response: ...


def create_app(dialect: str, engine: Engine) -> Starlette:
    server: DialectServer
    if dialect == "bigquery":
        server = BigQueryServer(engine)
    elif dialect == "snowflake":
        server = SnowflakeServer(engine)
    else:
        raise ValueError(f"unknown dialect {dialect!r}; expected one of {DIALECTS}")

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({**engine.health(), "dialect": dialect})

    async def reset(request: Request) -> JSONResponse:
        server.reset()
        engine.reset()
        return JSONResponse({"status": "reset"})

    async def fallback(request: Request) -> Response:
        return await server.not_implemented(request)

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    routes: list[BaseRoute] = [
        Route("/_crucible/health", health, methods=["GET"]),
        Route("/_crucible/reset", reset, methods=["POST"]),
        *server.routes(),
        Route("/{path:path}", fallback, methods=methods),
    ]
    app = Starlette(routes=routes)
    app.add_exception_handler(404, lambda request, exc: server.not_implemented(request))
    app.add_exception_handler(405, lambda request, exc: server.not_implemented(request))
    app.add_exception_handler(Exception, server.internal_error)
    return app
