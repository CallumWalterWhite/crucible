"""Split a BigQuery request into statements and route each one (docs/architecture.md)."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.annotate_types import annotate_types

from crucible.dialects.bigquery import errors, metadata, types
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
    SqlPlan,
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
        return SqlPlan(translate_query(tree, ctx).sql, "SELECT", params=ctx.params)
    if isinstance(tree, exp.Create) and tree.kind == "TABLE":
        return _create_table(tree, ctx)
    if isinstance(tree, exp.Insert):
        return _insert(tree, ctx)
    if isinstance(tree, exp.Command):
        raise CrucibleUnsupportedError(f"{tree.name.upper()} statement")
    raise CrucibleUnsupportedError(f"{type(tree).__name__.upper()} statement")


def _create_table(tree: exp.Create, ctx: ExecContext) -> Plan:
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    assert isinstance(table, exp.Table)
    if tree.args.get("properties"):
        # PARTITION BY / CLUSTER BY / OPTIONS / TEMP come with dbt's statements (slice 2).
        raise CrucibleUnsupportedError(
            f"CREATE TABLE options: {tree.args['properties'].sql('bigquery')}"
        )
    name = metadata.name_of(table, ctx)
    metadata.check_new_table(ctx, name)
    full = qualified(name.project, name.dataset, name.table)
    head = "CREATE OR REPLACE TABLE" if tree.args.get("replace") else "CREATE TABLE"
    if tree.args.get("exists"):
        head += " IF NOT EXISTS"

    query = tree.expression
    if isinstance(query, exp.Query):
        sql = f"{head} {full} AS {translate_query(query, ctx).sql}"
        statement_type = "CREATE_TABLE_AS_SELECT"
    elif isinstance(target, exp.Schema):
        sql = f"{head} {full} ({', '.join(_column_def(c) for c in target.expressions)})"
        statement_type = "CREATE_TABLE"
    else:
        raise errors.invalid("CREATE TABLE needs a column list or AS SELECT")

    def run(c: ExecContext) -> Result:
        result = c.run_sql(sql, returns_rows=False)
        c.catalog.set_properties(name.project, name.dataset, name.table, {})
        result.statement_type = statement_type
        return result

    return CommandPlan(run, statement_type)


def _column_def(column: exp.Expr) -> str:
    if not isinstance(column, exp.ColumnDef) or not isinstance(
        column.args.get("kind"), exp.DataType
    ):
        raise CrucibleUnsupportedError(f"column definition {column.sql('bigquery')}")
    not_null = False
    constraints: list[exp.ColumnConstraint] = column.args.get("constraints") or []
    for constraint in constraints:
        if isinstance(constraint.args.get("kind"), exp.NotNullColumnConstraint):
            not_null = True
        else:
            raise CrucibleUnsupportedError(f"column constraint {constraint.sql('bigquery')}")
    storage = types.storage_type(column.args["kind"]).sql("duckdb")
    return f"{qualified(column.name)} {storage}" + (" NOT NULL" if not_null else "")


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
    return SqlPlan(
        f"INSERT INTO {full} ({names}) {translation.sql}",
        "INSERT",
        params=ctx.params,
        returns_rows=False,
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
            if source_type is None or source_type.this == exp.DataType.Type.UNKNOWN:
                continue
            literal_string = isinstance(value, exp.Literal) and value.is_string
            if not types.coerces(source_type, target, literal_string=literal_string):
                raise errors.invalid(
                    f"Query column {index + 1} has type {types.type_name(source_type)} "
                    f"which cannot be inserted into column {columns_names[index]}, which has type "
                    f"{types.type_name(target)}"
                )
