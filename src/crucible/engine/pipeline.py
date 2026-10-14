"""route -> translate/handle -> execute -> results (docs/architecture.md, "Request pipeline")."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import duckdb
from sqlglot import exp

from crucible.engine.catalog import Catalog
from crucible.engine.errors import CrucibleError, ErrorKind, from_duckdb
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
        if any(_has_json(c.type) for c in columns):
            rows = [
                tuple(_decode(v, c.type) for v, c in zip(row, columns, strict=True)) for row in rows
            ]
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


def _has_json(data_type: exp.DataType) -> bool:
    return data_type.this == exp.DataType.Type.JSON or any(
        _has_json(t) for t in data_type.find_all(exp.DataType) if t is not data_type
    )


def _decode(value: Any, data_type: exp.DataType) -> Any:
    """DuckDB returns JSON as text; results carry the parsed value, as warehouse clients do."""
    if value is None:
        return None
    kind = data_type.this
    if kind == exp.DataType.Type.JSON:
        return json.loads(value)
    if kind == exp.DataType.Type.ARRAY:
        [inner] = data_type.expressions
        assert isinstance(inner, exp.DataType)
        return [_decode(v, inner) for v in value]
    if kind == exp.DataType.Type.STRUCT:
        out = dict(value)
        for field in data_type.expressions:
            if isinstance(field, exp.ColumnDef) and isinstance(
                field.args.get("kind"), exp.DataType
            ):
                out[field.name] = _decode(out.get(field.name), field.args["kind"])
        return out
    return value
