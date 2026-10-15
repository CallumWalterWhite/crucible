"""BigQuery DDL that dbt emits: CREATE SCHEMA, CREATE TABLE [AS SELECT], CREATE VIEW, ALTER TABLE,
DROP TABLE / VIEW.

Partitioning, clustering and OPTIONS have no effect on query results, so they are validated and
recorded as object properties (ADR 0002), not executed. An option that would change results if
ignored (require_partition_filter = TRUE) raises CrucibleUnsupportedError instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import sqlglot
from sqlglot import exp

from crucible.dialects.bigquery import errors, metadata, types
from crucible.dialects.bigquery.translate import translate_query
from crucible.engine import (
    CommandPlan,
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    Plan,
    Result,
)
from crucible.engine.catalog import qualified

_PARTITION_FUNCTIONS = {"DATE_TRUNC", "TIMESTAMP_TRUNC", "DATETIME_TRUNC", "RANGE_BUCKET"}
_MAX_CLUSTER_COLUMNS = 4


def _command(statement_type: str, run: Callable[[ExecContext], None]) -> Plan:
    def wrapped(c: ExecContext) -> Result:
        try:
            run(c)
        except CrucibleError as exc:
            raise errors.reword(exc) from exc
        return Result(statement_type)

    return CommandPlan(wrapped, statement_type)


def _check_columns(names: list[str], columns: list[str], clause: str) -> None:
    present = {c.lower() for c in columns}
    for name in names:
        if name.lower() not in present:
            raise errors.invalid(f"{clause} column {name} is not present in the table")


def _options(properties: list[exp.Expr]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for prop in properties:
        if not isinstance(prop, exp.Property):
            raise CrucibleUnsupportedError(f"table property {prop.sql('bigquery')}")
        value = prop.args.get("value")
        # BigQuery would reject queries without a partition filter; Crucible would not.
        if (
            prop.name.lower() == "require_partition_filter"
            and isinstance(value, exp.Boolean)
            and value.this
        ):
            raise CrucibleUnsupportedError("OPTIONS(require_partition_filter = TRUE)")
        if isinstance(value, exp.Literal) and value.is_string:
            options[prop.name] = value.this
        else:
            options[prop.name] = value.sql("bigquery") if isinstance(value, exp.Expr) else value
    return options


def table_properties(tree: exp.Create, columns: list[str]) -> dict[str, Any]:
    """PARTITION BY / CLUSTER BY / OPTIONS, validated against the table's columns."""
    recorded: dict[str, Any] = {}
    props = tree.args.get("properties")
    if not isinstance(props, exp.Properties):
        return recorded
    option_props: list[exp.Expr] = []
    for prop in props.expressions:
        if isinstance(prop, exp.TemporaryProperty):
            raise CrucibleUnsupportedError("CREATE TEMP TABLE (BigQuery scripting)")
        if isinstance(prop, exp.PartitionedByProperty):
            expression = prop.this
            if isinstance(expression, exp.Identifier):
                expression = exp.column(expression.copy())
            if isinstance(expression, exp.Column):
                if expression.name.upper().startswith("_PARTITION"):
                    raise CrucibleUnsupportedError("ingestion-time partitioning")
                referenced = [expression.name]
            elif isinstance(expression, exp.Func) and (
                expression.sql_name().upper() in _PARTITION_FUNCTIONS
                or (
                    isinstance(expression, exp.Anonymous)
                    and expression.name.upper() in _PARTITION_FUNCTIONS
                )
            ):
                referenced = [c.name for c in expression.find_all(exp.Column)][:1]
            else:
                raise CrucibleUnsupportedError(f"PARTITION BY {expression.sql('bigquery')}")
            _check_columns(referenced, columns, "PARTITION BY")
            recorded["partition_by"] = expression.sql("bigquery")
        elif isinstance(prop, exp.ClusterProperty):
            names = [e.name for e in prop.expressions]
            if len(names) > _MAX_CLUSTER_COLUMNS:
                raise errors.invalid(f"Too many clustering columns: {len(names)}, maximum is 4")
            _check_columns(names, columns, "CLUSTER BY")
            recorded["cluster_by"] = names
        else:
            option_props.append(prop)
    if option_props:
        recorded["options"] = _options(option_props)
    return recorded


def rest_partitioning(properties: dict[str, Any], *, definition: bool = True) -> dict[str, Any]:
    """A table's recorded PARTITION BY / CLUSTER BY as REST Table fields (REST reference, Table:
    timePartitioning, rangePartitioning, clustering), for tables.get and tables.list."""
    out: dict[str, Any] = {}
    text = properties.get("partition_by")
    if isinstance(text, str):
        node = sqlglot.parse_one(text, read="bigquery")
        if isinstance(node, exp.Column):  # a DATE column: daily partitions
            out["timePartitioning"] = {"type": "DAY", "field": node.name}
        elif isinstance(node, exp.RangeBucket):
            bounds = node.expression
            assert isinstance(bounds, exp.GenerateSeries)
            step = bounds.args.get("step")
            out["rangePartitioning"] = {
                "field": node.this.name,
                "range": {
                    "start": str(bounds.args["start"].to_py()),
                    "end": str(bounds.args["end"].to_py()),
                    "interval": str(step.to_py() if step is not None else 1),
                },
            }
        else:  # DATE_TRUNC / TIMESTAMP_TRUNC / DATETIME_TRUNC (column, unit)
            unit = node.args.get("unit")
            out["timePartitioning"] = {
                "type": unit.name.upper() if isinstance(unit, exp.Expr) else "DAY",
                "field": node.this.name,
            }
    # Recorded in tables.get next to either kind of partitioning; not in tables.list entries.
    for kind in ("timePartitioning", "rangePartitioning"):
        if kind in out and definition:
            out["partitionDefinition"] = {"partitionedColumn": [{"field": out[kind]["field"]}]}
    clustering = properties.get("cluster_by")
    if isinstance(clustering, list) and clustering:
        out["clustering"] = {"fields": [str(f) for f in cast("list[Any]", clustering)]}
    return out


def plan_create(tree: exp.Create, ctx: ExecContext) -> Plan:
    kind = (tree.kind or "").upper()
    if kind == "SCHEMA":
        return _create_schema(tree, ctx)
    if kind == "TABLE":
        return _create_table(tree, ctx)
    if kind == "VIEW":
        return _create_view(tree, ctx)
    raise CrucibleUnsupportedError(f"CREATE {kind}")


def _create_schema(tree: exp.Create, ctx: ExecContext) -> Plan:
    target = tree.this
    assert isinstance(target, exp.Table)
    # `p`.`d` parses with the dataset as `db` and the project as `catalog`; plain `d` as the name.
    project = (target.catalog if target.name == "" else target.db) or ctx.session.default_catalog
    dataset = target.db if target.name == "" else target.name
    if not project:
        raise errors.invalid(f"Dataset {dataset} needs a project")
    if dataset.lower() == "main":
        raise CrucibleUnsupportedError("a dataset named main (reserved by DuckDB)")
    options = (
        _options(list(tree.args["properties"].expressions)) if tree.args.get("properties") else {}
    )
    if_not_exists = bool(tree.args.get("exists"))

    def run(c: ExecContext) -> None:
        existing = c.catalog.schemas(project) if c.catalog.catalog_exists(project) else []
        same = [s for s in existing if s.lower() == dataset.lower()]
        if same and same[0] != dataset:
            raise CrucibleUnsupportedError(
                f"dataset {dataset} next to {same[0]}: names that differ only by case"
            )
        if same:
            if if_not_exists:
                return
            raise CrucibleError(
                ErrorKind.ALREADY_EXISTS, f"Already Exists: Dataset {project}:{dataset}"
            )
        c.catalog.ensure_schema(project, dataset)
        c.catalog.set_properties(project, dataset, "", {"options": options} if options else {})

    return _command("CREATE_SCHEMA", run)


def _query_of(expression: exp.Expr) -> exp.Expr:
    return expression.this if isinstance(expression, exp.Subquery) else expression


def _create_table(tree: exp.Create, ctx: ExecContext) -> Plan:
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    assert isinstance(table, exp.Table)
    if tree.args.get("temporary"):
        raise CrucibleUnsupportedError("CREATE TEMP TABLE (BigQuery scripting)")
    name = metadata.name_of(table, ctx)
    metadata.check_new_table(ctx, name)
    full = qualified(name.project, name.dataset, name.table)
    head = "CREATE OR REPLACE TABLE" if tree.args.get("replace") else "CREATE TABLE"
    if tree.args.get("exists"):
        head += " IF NOT EXISTS"

    query = tree.expression
    if isinstance(query, exp.Expr) and isinstance(_query_of(query), exp.Query):
        translation = translate_query(_query_of(query), ctx)
        sql = f"{head} {full} AS {translation.sql}"
        columns = translation.names
        statement_type = "CREATE_TABLE_AS_SELECT"
    elif isinstance(target, exp.Schema):
        sql = f"{head} {full} ({', '.join(column_def(c) for c in target.expressions)})"
        columns = [c.name for c in target.expressions]
        statement_type = "CREATE_TABLE"
    else:
        raise errors.invalid("CREATE TABLE needs a column list or AS SELECT")
    properties = table_properties(tree, columns)

    def run(c: ExecContext) -> None:
        c.run_sql(sql, returns_rows=False)
        c.catalog.set_properties(name.project, name.dataset, name.table, properties)

    return _command(statement_type, run)


def _create_view(tree: exp.Create, ctx: ExecContext) -> Plan:
    table = tree.this
    assert isinstance(table, exp.Table)
    name = metadata.name_of(table, ctx)
    metadata.check_new_table(ctx, name)
    query = tree.expression
    if not isinstance(query, exp.Expr) or not isinstance(_query_of(query), exp.Query):
        raise errors.invalid("CREATE VIEW needs AS SELECT")
    translation = translate_query(_query_of(query), ctx)
    head = "CREATE OR REPLACE VIEW" if tree.args.get("replace") else "CREATE VIEW"
    if tree.args.get("exists"):
        head += " IF NOT EXISTS"
    sql = f"{head} {qualified(name.project, name.dataset, name.table)} AS {translation.sql}"
    properties = table_properties(tree, translation.names)

    def run(c: ExecContext) -> None:
        c.run_sql(sql, returns_rows=False)
        c.catalog.set_properties(name.project, name.dataset, name.table, properties)

    return _command("CREATE_VIEW", run)


def column_def(column: exp.Expr) -> str:
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


def plan_alter(tree: exp.Alter, ctx: ExecContext) -> Plan:
    if (tree.args.get("kind") or "").upper() != "TABLE":
        raise CrucibleUnsupportedError(f"ALTER {tree.args.get('kind')}")
    table = tree.this
    assert isinstance(table, exp.Table)
    info = metadata.find_table(ctx, metadata.name_of(table, ctx))
    full = qualified(info.catalog, info.schema, info.name)
    statements: list[str] = []
    options: dict[str, Any] = {}
    actions: list[exp.Expr] = tree.args.get("actions") or []
    for action in actions:
        if isinstance(action, exp.ColumnDef):
            if action.args.get("constraints"):
                raise errors.invalid(
                    f"ALTER TABLE ADD COLUMN cannot add a REQUIRED column {action.name}"
                )
            if_not_exists = " IF NOT EXISTS" if action.args.get("exists") else ""
            storage = types.storage_type(action.args["kind"]).sql("duckdb")
            statements.append(
                f"ALTER TABLE {full} ADD COLUMN{if_not_exists} {qualified(action.name)} {storage}"
            )
        elif isinstance(action, exp.AlterSet):
            # dbt sends `SET OPTIONS()` with no options; SQLGlot parses that as [None].
            for group in (g for g in action.expressions if g is not None):
                props = group.expressions if isinstance(group, exp.Properties) else [group]
                options.update(_options(list(props)))
        else:
            raise CrucibleUnsupportedError(f"ALTER TABLE action {action.sql('bigquery')}")

    def run(c: ExecContext) -> None:
        for sql in statements:
            c.run_sql(sql, returns_rows=False)
        if options:
            properties = c.catalog.properties(info.catalog, info.schema, info.name)
            merged = {**properties.get("options", {}), **options}
            c.catalog.set_properties(
                info.catalog, info.schema, info.name, {**properties, "options": merged}
            )

    return _command("ALTER_TABLE", run)


def plan_drop(tree: exp.Drop, ctx: ExecContext) -> Plan:
    kind = (tree.args.get("kind") or "").upper()
    if kind not in ("TABLE", "VIEW"):
        raise CrucibleUnsupportedError(f"DROP {kind}")
    tables = tree.args.get("tables") or [tree.this]
    if len(tables) != 1 or not isinstance(tables[0], exp.Table):
        raise CrucibleUnsupportedError("DROP of several objects in one statement")
    name = metadata.name_of(tables[0], ctx)
    if_exists = bool(tree.args.get("exists"))

    def run(c: ExecContext) -> None:
        try:
            info = metadata.find_table(c, name)
        except CrucibleError:
            if if_exists:
                return
            raise
        if (info.kind == "view") != (kind == "VIEW"):
            raise errors.invalid(f"{info.name} is a {info.kind}; use DROP {info.kind.upper()}")
        c.run_sql(
            f"DROP {kind} {qualified(info.catalog, info.schema, info.name)}", returns_rows=False
        )
        c.catalog.clear_properties(info.catalog, info.schema, info.name)

    return _command(f"DROP_{kind}", run)
