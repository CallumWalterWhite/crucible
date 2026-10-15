"""Split a BigQuery request into statements and route each one (docs/architecture.md)."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.annotate_types import annotate_types

from crucible.dialects.bigquery import ddl, dml, errors, metadata, types
from crucible.dialects.bigquery.translate import (
    fix_annotation,
    outer_selects,
    schema_for,
    translate_query,
)
from crucible.engine import (
    CommandPlan,
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    Location,
    Plan,
    Result,
)
from crucible.engine.catalog import qualified


@dataclass(frozen=True)
class Statement:
    tree: exp.Expr


def split(sql: str) -> list[object]:
    try:
        trees = sqlglot.parse(sql, read="bigquery")
    except ParseError as exc:
        detail = exc.errors[0] if exc.errors else {}
        line, col = detail.get("line"), detail.get("col")
        location = Location(int(line), int(col)) if line and col else None
        message = str(detail.get("description") or exc).strip()
        raise CrucibleError(
            ErrorKind.INVALID_QUERY, f"Syntax error: {message}", location=location
        ) from exc
    statements: list[object] = []
    statements.extend(Statement(t) for t in trees if t is not None)
    if not statements:
        raise errors.invalid("Syntax error: the request has no statement")
    return statements


def plan(statement: object, ctx: ExecContext) -> Plan:
    assert isinstance(statement, Statement)
    tree = statement.tree.copy()
    if isinstance(tree, exp.Query):
        return run_sql(translate_query(tree, ctx).sql, "SELECT", ctx)
    if isinstance(tree, exp.Create):
        return ddl.plan_create(tree, ctx)
    if isinstance(tree, exp.Alter):
        return ddl.plan_alter(tree, ctx)
    if isinstance(tree, exp.Drop):
        return ddl.plan_drop(tree, ctx)
    if isinstance(tree, exp.Merge):
        return dml.plan_merge(tree, ctx)
    if isinstance(tree, exp.Insert):
        return _insert(tree, ctx)
    if isinstance(tree, exp.Command):
        raise CrucibleUnsupportedError(f"{tree.name.upper()} statement")
    raise CrucibleUnsupportedError(f"{type(tree).__name__.upper()} statement")


def run_sql(sql: str, statement_type: str, ctx: ExecContext, *, returns_rows: bool = True) -> Plan:
    """Run generated SQL, giving DuckDB's own runtime errors BigQuery's wording."""
    params = ctx.params

    def run(c: ExecContext) -> Result:
        try:
            result = c.run_sql(sql, params, returns_rows=returns_rows)
        except CrucibleError as exc:
            raise errors.reword(exc) from exc
        result.statement_type = statement_type
        return result

    return CommandPlan(run, statement_type)


def _insert(tree: exp.Insert, ctx: ExecContext) -> Plan:
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    assert isinstance(table, exp.Table)
    info = metadata.find_table(ctx, metadata.name_of(table, ctx))
    by_name = {c.name.lower(): c for c in info.columns}
    if isinstance(target, exp.Schema):
        try:
            columns = [by_name[c.name.lower()] for c in target.expressions]
        except KeyError as exc:
            raise errors.invalid(
                f"Column {exc.args[0]} is not present in table {info.name}"
            ) from exc
    else:
        columns = list(info.columns)
    target_types = [types.parse(types.type_name(c.type)) for c in columns]

    source = tree.expression
    if isinstance(source, exp.Values):
        source = _values_as_select(source)
    if not isinstance(source, exp.Query):
        raise CrucibleUnsupportedError(f"INSERT source {source.sql('bigquery')}")
    _check_assignable(
        source, columns_names=[c.name for c in columns], target_types=target_types, ctx=ctx
    )

    translation = translate_query(source, ctx, target_types=target_types)
    names = ", ".join(qualified(c.name) for c in columns)
    full = qualified(info.catalog, info.schema, info.name)
    return run_sql(
        f"INSERT INTO {full} ({names}) {translation.sql}", "INSERT", ctx, returns_rows=False
    )


def _values_as_select(values: exp.Values) -> exp.Query:
    rows = [row.expressions if isinstance(row, exp.Tuple) else [row] for row in values.expressions]
    query: exp.Query = exp.select(*rows[0])
    for row in rows[1:]:
        query = exp.union(query, exp.select(*row), distinct=False)
    return query


def _check_assignable(
    source: exp.Query, columns_names: list[str], target_types: list[exp.DataType], ctx: ExecContext
) -> None:
    """BigQuery refuses to INSERT a value whose type does not coerce to the column type."""
    probe = source.copy()
    infos = metadata.qualify_names(probe, ctx)
    annotate_types(probe, schema=schema_for(infos), dialect="bigquery")
    fix_annotation(probe)
    for select in outer_selects(probe):
        if len(select.expressions) != len(target_types):
            raise errors.invalid(
                f"Inserted row has wrong column count; Has {len(select.expressions)}, "
                f"expected {len(target_types)}"
            )
        for index, (projection, target) in enumerate(
            zip(select.expressions, target_types, strict=True)
        ):
            value = projection.this if isinstance(projection, exp.Alias) else projection
            source_type = value.type
            # A literal NULL is assignable to any column (SQLGlot types a bare NULL as INT64).
            if isinstance(value, exp.Null) or source_type is None:
                continue
            if source_type.this == exp.DataType.Type.UNKNOWN:
                continue
            literal_string = isinstance(value, exp.Literal) and value.is_string
            if not types.coerces(source_type, target, literal_string=literal_string):
                raise errors.invalid(
                    f"Query column {index + 1} has type {types.type_name(source_type)} "
                    f"which cannot be inserted into column {columns_names[index]}, which has type "
                    f"{types.type_name(target)}"
                )
