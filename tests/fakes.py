"""Test-only fakes shared by unit and corpus tests."""

from __future__ import annotations

from typing import Any

from sqlglot import exp

from crucible.engine import CommandPlan, ExecContext, Plan, Result, Session, SqlPlan


class PassthroughDialect:
    """Statements are DuckDB SQL separated by ';'. 'BEGIN'/'COMMIT'/'ROLLBACK' are commands."""

    name = "passthrough"

    def new_session(self, default_catalog: str | None = None, default_schema: str | None = None):
        return Session(default_catalog=default_catalog, default_schema=default_schema)

    def split(self, sql: str) -> list[object]:
        return [s.strip() for s in sql.split(";") if s.strip()]

    def plan(self, statement: object, ctx: ExecContext) -> Plan:
        text = str(statement)
        verb = text.split()[0].upper()
        if verb in ("BEGIN", "COMMIT", "ROLLBACK"):
            action = {"BEGIN": ctx.begin, "COMMIT": ctx.commit, "ROLLBACK": ctx.rollback}[verb]

            def run(c: ExecContext) -> Result:
                action()
                return Result(verb)

            return CommandPlan(run, verb)
        returns_rows = verb in ("SELECT", "WITH", "FROM")
        return SqlPlan(text, verb, params=ctx.params, returns_rows=returns_rows)

    def warehouse_type(self, duckdb_type: exp.DataType) -> str:
        return duckdb_type.sql("duckdb")

    def canonical_value(self, value: Any, warehouse_type: str) -> Any:
        return value
