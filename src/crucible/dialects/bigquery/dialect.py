"""The BigQuery dialect: the Dialect protocol (dialects/base.py) for BigQuery SQL."""

from __future__ import annotations

from typing import Any

from sqlglot import exp

from crucible.dialects.bigquery import statements, types
from crucible.engine import ExecContext, Plan, Session


class BigQueryDialect:
    name = "bigquery"

    def new_session(
        self, default_catalog: str | None = None, default_schema: str | None = None
    ) -> Session:
        # BigQuery evaluates time functions in UTC unless a call names a timezone (ADR 0012).
        return Session(
            default_catalog=default_catalog, default_schema=default_schema, timezone="UTC"
        )

    def split(self, sql: str) -> list[object]:
        return statements.split(sql)

    def plan(self, statement: object, ctx: ExecContext) -> Plan:
        return statements.plan(statement, ctx)

    def warehouse_type(self, duckdb_type: exp.DataType) -> str:
        return types.type_name(duckdb_type)

    def canonical_value(self, value: Any, warehouse_type: str) -> Any:
        return types.canonical_value(value, warehouse_type)


DIALECT = BigQueryDialect()
