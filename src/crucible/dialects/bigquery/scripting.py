"""The BigQuery scripting subset dbt-bigquery emits: DECLARE, SET and variable references.

dbt-bigquery 1.9 and 1.12's dynamic `insert_overwrite` and `microbatch` scripts declare
`dbt_partitions_for_replacement ARRAY<...>` (and `_dbt_max_partition ... DEFAULT (SELECT ...)` when
the model reads it), SET it from `SELECT AS STRUCT ARRAY_AGG(...)`, and read it in
`... IN UNNEST(var)` (macros/materializations/incremental_strategy/insert_overwrite.sql and
common.sql).
A variable lives for the request (the script). Each reference becomes `CAST($v_<name> AS <type>)`
and its value is bound by DuckDB, like a query parameter. BEGIN ... END, IF, loops, EXECUTE
IMMEDIATE and temp tables stay refused
(https://cloud.google.com/bigquery/docs/reference/standard-sql/procedural-language).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlglot import exp

from crucible.dialects.bigquery import errors
from crucible.engine import (
    CommandPlan,
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    Plan,
    Result,
)

STATE = "script_variables"  # session.state: variable name -> BigQuery type SQL


def bind_name(name: str) -> str:
    return f"v_{name.lower()}"


def declared(ctx: ExecContext) -> dict[str, str]:
    return ctx.session.state.setdefault(STATE, {})


def _assign(ctx: ExecContext, name: str, value: Any) -> None:
    if not isinstance(ctx.params, dict):
        ctx.params = dict(ctx.params or {})  # pyright: ignore[reportArgumentType]
    params: dict[str, Any] = ctx.params  # pyright: ignore[reportAssignmentType]
    params[bind_name(name)] = value


def bind(query: exp.Expr, ctx: ExecContext, columns: set[str]) -> None:
    """Replace unqualified column references that name a declared variable (and no column of
    the query's tables: a column wins, as in BigQuery) with the variable's typed placeholder."""
    variables = declared(ctx)
    if not variables:
        return
    for column in list(query.find_all(exp.Column)):
        name = column.name.lower()
        if column.table or name not in variables or name in columns:
            continue
        kind = exp.DataType.build(variables[name], dialect="bigquery")
        column.replace(exp.Cast(this=exp.Placeholder(this=bind_name(name)), to=kind))


def plan_declare(
    tree: exp.Declare, ctx: ExecContext, plan_query: Callable[[exp.Expr], Plan]
) -> Plan:
    def run(c: ExecContext) -> Result:
        for item in tree.expressions:
            assert isinstance(item, exp.DeclareItem)
            names: list[exp.Identifier] = item.this
            kind = item.args.get("kind")
            default = item.args.get("default")
            if kind is None and not isinstance(default, exp.Expr):
                raise errors.invalid_at_statement(tree, "Variable declaration needs a type")
            value: Any = None
            type_sql = kind.sql("bigquery") if isinstance(kind, exp.DataType) else None
            if isinstance(default, exp.Expr):
                result = c.run(plan_query(exp.select(_typed(default, kind))))
                value = result.rows[0][0] if result.rows else None
                if type_sql is None:
                    type_sql = c.dialect.warehouse_type(result.columns[0].type)
            variables = declared(c)
            for identifier in names:
                name = identifier.name.lower()
                if name in variables:
                    # recorded: "Variable 'x' redeclaration at [1:26]", at the second name
                    raise errors.invalid_at(
                        identifier, f"Variable '{identifier.name}' redeclaration"
                    )
                assert type_sql is not None
                variables[name] = type_sql
                _assign(c, name, value)
        return Result("DECLARE", info={"queried": _reads_table(tree)})

    return CommandPlan(run, "DECLARE")


def plan_set(tree: exp.Set, ctx: ExecContext, plan_query: Callable[[exp.Expr], Plan]) -> Plan:
    def run(c: ExecContext) -> Result:
        variables = declared(c)
        for item in tree.expressions:
            assignment = item.this if isinstance(item, exp.SetItem) else None
            if not isinstance(assignment, exp.EQ):
                raise CrucibleUnsupportedError(f"SET {item.sql('bigquery')}")
            target = assignment.this
            targets = target.expressions if isinstance(target, exp.Tuple) else [target]
            targets = [t.this if isinstance(t, exp.Paren) else t for t in targets]
            columns = [t for t in targets if isinstance(t, exp.Column)]
            if len(columns) != len(targets):
                raise CrucibleUnsupportedError(f"SET target {target.sql('bigquery')}")
            names = [t.name.lower() for t in columns]
            for column in columns:
                if column.name.lower() not in variables:
                    # recorded: "Undeclared variable: y at [1:5]", at the name
                    raise errors.invalid_at(column.this, f"Undeclared variable: {column.name}")
            source = assignment.expression
            if len(names) == 1 and not _is_struct_subquery(source):
                kind = exp.DataType.build(variables[names[0]], dialect="bigquery")
                result = c.run(plan_query(exp.select(_typed(source, kind))))
                _assign(c, names[0], result.rows[0][0] if result.rows else None)
                continue
            if not _is_struct_subquery(source):
                raise CrucibleUnsupportedError("SET of several variables from a non-STRUCT value")
            select = source.this.copy()
            select.set("kind", None)
            result = c.run(plan_query(select))
            if len(result.rows) > 1:
                raise CrucibleError(
                    ErrorKind.RUNTIME, "Scalar subquery produced more than one element"
                )
            row = result.rows[0] if result.rows else (None,) * len(names)
            if len(row) != len(names):
                raise errors.invalid_at_statement(
                    tree, f"SET assigns {len(names)} variables from {len(row)} values"
                )
            for name, value in zip(names, row, strict=True):
                _assign(c, name, value)
        return Result("SET", info={"queried": _reads_table(tree)})

    return CommandPlan(run, "SET")


def _reads_table(tree: exp.Expr) -> bool:
    """A DECLARE or SET whose value is a subquery runs as a child job of the script."""
    return tree.find(exp.Select) is not None


def _is_struct_subquery(node: exp.Expr) -> bool:
    return (
        isinstance(node, exp.Subquery)
        and isinstance(node.this, exp.Select)
        and str(node.this.args.get("kind") or "").upper() == "STRUCT"
    )


def _typed(value: exp.Expr, kind: object) -> exp.Expr:
    """The value, cast to the variable's declared type when it has one."""
    out = value.copy()
    if isinstance(kind, exp.DataType):
        return exp.alias_(exp.Cast(this=out, to=kind.copy()), "v")
    return exp.alias_(out, "v")
