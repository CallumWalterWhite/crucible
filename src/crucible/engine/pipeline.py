"""route -> translate/handle -> execute -> results (docs/architecture.md, "Request pipeline")."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import duckdb

from crucible.engine.catalog import Catalog
from crucible.engine.errors import CrucibleError, CrucibleUnsupportedError, ErrorKind, from_duckdb
from crucible.engine.session import Session
from crucible.engine.types import Column, parse_type

if TYPE_CHECKING:
    from crucible.dialects.base import Dialect
    from crucible.engine.engine import Engine

Params = Mapping[str, Any] | Sequence[Any] | None


@dataclass
class Result:
    """One statement's outcome. Column types are DuckDB types; dialects map them."""

    statement_type: str
    columns: list[Column] = field(default_factory=list[Column])
    rows: list[tuple[Any, ...]] = field(default_factory=list[tuple[Any, ...]])
    rowcount: int | None = None  # rows affected by DML
    info: dict[str, Any] = field(default_factory=dict[str, Any])  # dialect-specific details


@dataclass(frozen=True)
class SqlPlan:
    """Run DuckDB SQL produced by the dialect's translation."""

    sql: str
    statement_type: str
    params: Params = None
    returns_rows: bool = True
    info: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class CommandPlan:
    """Run a handler: commands (SHOW/USE/...), DDL with side effects, and scripts."""

    run: Callable[[ExecContext], Result]
    statement_type: str
    # SQL whose columns are the result's, so a dry run can describe it without running `run`.
    columns_sql: str | None = None


Plan = SqlPlan | CommandPlan


class ExecContext:
    """What a dialect sees while planning and running one request."""

    def __init__(
        self,
        engine: Engine,
        dialect: Dialect,
        session: Session,
        cursor: duckdb.DuckDBPyConnection,
        params: Params,
    ) -> None:
        self.engine = engine
        self.dialect = dialect
        self.session = session
        self.cursor = cursor
        self.params = params
        # The live schema the translation qualifies against (ADR 0010).
        self.catalog: Catalog = engine.catalog(cursor)

    def execute(self, statement: object) -> Result:
        """Plan and run one statement. Scripts call this for each child statement."""
        return self.run(self.dialect.plan(statement, self))

    def run(self, plan: Plan) -> Result:
        if isinstance(plan, CommandPlan):
            return plan.run(self)
        result = self.run_sql(plan.sql, plan.params, returns_rows=plan.returns_rows)
        result.statement_type = plan.statement_type
        result.info.update(plan.info)
        return result

    def run_sql(self, sql: str, params: Params = None, *, returns_rows: bool = True) -> Result:
        try:
            self.cursor.execute(sql, params)
            description = self.cursor.description or []
            rows = self.cursor.fetchall() if description else []
        except duckdb.Error as exc:
            raise from_duckdb(exc) from exc
        if not returns_rows:
            # DuckDB reports DML as one 'Count' row and DDL as no rows.
            return Result("", rowcount=rows[0][0] if rows else None)
        columns = [Column(d[0], parse_type(str(d[1]))) for d in description]
        return Result("", columns=columns, rows=rows)

    # --- explicit transactions (ADR 0003) ---------------------------------------------------
    def begin(self) -> None:
        if self.session.transaction is not None:
            raise CrucibleError(ErrorKind.INVALID_QUERY, "A transaction is already open")
        self.run_sql("begin transaction", returns_rows=False)
        self.session.transaction = self.cursor

    def commit(self) -> None:
        self._end("commit")

    def rollback(self) -> None:
        self._end("rollback")

    def _end(self, verb: str) -> None:
        if self.session.transaction is None:
            raise CrucibleError(ErrorKind.INVALID_QUERY, f"No transaction is open to {verb}")
        try:
            self.run_sql(verb, returns_rows=False)
        finally:
            self.session.transaction = None


class Pipeline:
    def __init__(self, engine: Engine, dialect: Dialect) -> None:
        self.engine = engine
        self.dialect = dialect

    def execute(self, session: Session, sql: str, params: Params = None) -> list[Result]:
        statements = self.dialect.split(sql)
        with self.engine.cursor(session) as cursor:
            ctx = ExecContext(self.engine, self.dialect, session, cursor, params)
            return [ctx.execute(s) for s in statements]

    def dry_run(self, session: Session, sql: str, params: Params = None) -> Result:
        """Plan one statement without running it: its type and, for a query, its columns.

        A query's columns come from DuckDB's DESCRIBE, which binds the SQL without executing it
        (so runtime errors, such as a division by zero, are not raised, as in a real dry run).
        """
        statements = self.dialect.split(sql)
        if len(statements) != 1:
            raise CrucibleUnsupportedError("a dry run of more than one statement")
        with self.engine.cursor(session) as cursor:
            ctx = ExecContext(self.engine, self.dialect, session, cursor, params)
            plan = self.dialect.plan(statements[0], ctx)
            result = Result(plan.statement_type)
            describe: str | None = (
                plan.sql if isinstance(plan, SqlPlan) and plan.returns_rows else None
            )
            if isinstance(plan, CommandPlan):
                describe = plan.columns_sql
            if describe is not None:
                described = ctx.run_sql(f"DESCRIBE {describe}", params).rows
                result.columns = [Column(str(r[0]), parse_type(str(r[1]))) for r in described]
            return result
