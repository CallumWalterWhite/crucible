"""The Dialect protocol: the only contract the engine knows (ADR 0010).

A dialect turns warehouse SQL into plans the engine runs, maps DuckDB types to warehouse
types, and renders values canonically for the corpus. Translation is schema-aware: `plan`
receives the ExecContext, whose `catalog` is the live schema used to qualify and type the AST.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlglot import exp

from crucible.engine.pipeline import ExecContext, Plan
from crucible.engine.session import Session


class Dialect(Protocol):
    name: str  # "bigquery" | "snowflake"

    def new_session(
        self, default_catalog: str | None = None, default_schema: str | None = None
    ) -> Session:
        """A session with this warehouse's defaults (timezone, case rules state, ...)."""
        ...

    def split(self, sql: str) -> list[object]:
        """Parse a request into statements (a multi-statement script may be one statement).

        Raises CrucibleError(INVALID_QUERY) for syntax errors and CrucibleUnsupportedError for
        constructs Crucible does not emulate, both with a location.
        """
        ...

    def plan(self, statement: object, ctx: ExecContext) -> Plan:
        """parse tree -> qualify against ctx.catalog -> annotate types -> transforms -> generate."""
        ...

    def warehouse_type(self, duckdb_type: exp.DataType) -> str:
        """The warehouse type name for a DuckDB result type, e.g. BIGINT -> 'INT64'."""
        ...

    def canonical_value(self, value: Any, warehouse_type: str) -> Any:
        """A JSON-safe rendering used by the corpus to compare Crucible with the warehouse."""
        ...
