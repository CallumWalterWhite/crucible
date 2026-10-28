"""BigQuery DDL that dbt emits: CREATE SCHEMA, CREATE TABLE [AS SELECT], CREATE VIEW, ALTER TABLE,
DROP TABLE / VIEW.

Partitioning, clustering and OPTIONS have no effect on query results, so they are validated and
recorded as object properties (ADR 0002), not executed. An option that would change results if
ignored (require_partition_filter = TRUE) raises CrucibleUnsupportedError instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

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

# The column type each PARTITION BY form takes, and the units it allows ("" for none)
# (https://cloud.google.com/bigquery/docs/creating-partitioned-tables). A bare column is DATE.
_PARTITION_FORMS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "": (frozenset({"DATE"}), frozenset({""})),
    "DATE": (frozenset({"TIMESTAMP", "DATETIME"}), frozenset({""})),
    "DATE_TRUNC": (frozenset({"DATE"}), frozenset({"MONTH", "YEAR"})),
    "TIMESTAMP_TRUNC": (frozenset({"TIMESTAMP"}), frozenset({"DAY", "HOUR", "MONTH", "YEAR"})),
    "DATETIME_TRUNC": (frozenset({"DATETIME"}), frozenset({"DAY", "HOUR", "MONTH", "YEAR"})),
    "RANGE_BUCKET": (frozenset({"INT64"}), frozenset({""})),
}
_PARTITION_RULE = (
    "PARTITION BY expression must be DATE(<timestamp_column>), DATE(<datetime_column>), "
    "DATETIME_TRUNC(<datetime_column>, DAY/HOUR/MONTH/YEAR), a DATE-type column, "
    "TIMESTAMP_TRUNC(<timestamp_column>, DAY/HOUR/MONTH/YEAR), DATE_TRUNC(<date_column>, "
    "MONTH/YEAR), or RANGE_BUCKET(<int64_column>, GENERATE_ARRAY(<int64_value>, <int64_value>[, "
    "<int64_value>]))"
)
_MAX_CLUSTER_COLUMNS = 4
# Clustering columns are top-level, non-repeated columns of these types
# (https://cloud.google.com/bigquery/docs/clustered-tables#cluster_column_types).
_CLUSTER_TYPES = frozenset(
    {"BIGNUMERIC", "BOOL", "DATE", "DATETIME", "GEOGRAPHY", "INT64", "NUMERIC", "RANGE", "STRING",
     "TIMESTAMP"}
)  # fmt: skip

Columns = dict[str, exp.DataType | None]  # lowercased name -> BigQuery type (None: not known)


def _command(statement_type: str, run: Callable[[ExecContext], None]) -> Plan:
    def wrapped(c: ExecContext) -> Result:
        try:
            run(c)
        except CrucibleError as exc:
            raise errors.reword(exc) from exc
        return Result(statement_type)

    return CommandPlan(wrapped, statement_type)


def _check_columns(names: list[str], columns: Columns, clause: str) -> None:
    for name in names:
        if name.lower() not in columns:
            raise errors.invalid(f"{clause} column {name} is not present in the table")


def _type_of(name: str, columns: Columns) -> str | None:
    kind = columns.get(name.lower())
    if kind is None or kind.this == exp.DType.UNKNOWN:
        return None
    return types.type_name(kind).split("<", 1)[0].split("(", 1)[0]


def _check_partition(expression: exp.Expr, columns: Columns) -> list[str]:
    """The columns a PARTITION BY expression reads, after checking its form, type and unit."""
    if isinstance(expression, exp.Column):
        if expression.name.upper().startswith("_PARTITION"):
            raise CrucibleUnsupportedError("ingestion-time partitioning")
        form, column, unit = "", expression, ""
    elif isinstance(expression, exp.Func):
        form = expression.name if isinstance(expression, exp.Anonymous) else expression.sql_name()
        form = form.upper()
        column = expression.this
        unit_arg = expression.args.get("unit")
        unit = unit_arg.name.upper() if isinstance(unit_arg, exp.Expr) else ""
    else:
        raise CrucibleUnsupportedError(f"PARTITION BY {expression.sql('bigquery')}")
    if form not in _PARTITION_FORMS:
        raise CrucibleUnsupportedError(f"PARTITION BY {expression.sql('bigquery')}")
    if not isinstance(column, exp.Column):
        raise errors.invalid(_PARTITION_RULE)
    _check_columns([column.name], columns, "PARTITION BY")
    allowed_types, allowed_units = _PARTITION_FORMS[form]
    kind = _type_of(column.name, columns)
    if unit not in allowed_units or (kind is not None and kind not in allowed_types):
        raise errors.invalid(_PARTITION_RULE)
    return [column.name]


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
        elif isinstance(value, exp.Null):
            options[prop.name] = None  # OPTIONS(x = NULL) clears x in ALTER ... SET OPTIONS
        elif prop.name.lower() == "labels" and isinstance(value, exp.Array):
            options[prop.name] = _labels(value)
        elif prop.name.lower() == "description":
            raise CrucibleUnsupportedError("OPTIONS(description) that is not a string")
        else:
            options[prop.name] = value.sql("bigquery") if isinstance(value, exp.Expr) else value
    return options


def _labels(array: exp.Array) -> dict[str, str]:
    """OPTIONS(labels = [(key, value), ...]): the labels as a map (BigQuery reference, labels)."""
    labels: dict[str, str] = {}
    for pair in array.expressions:
        items = pair.expressions if isinstance(pair, exp.Tuple) else []
        if len(items) != 2 or not all(isinstance(i, exp.Literal) and i.is_string for i in items):
            raise CrucibleUnsupportedError("OPTIONS(labels) that is not (key, value) string pairs")
        labels[items[0].this] = items[1].this
    return labels


def table_properties(tree: exp.Create, columns: Columns) -> dict[str, Any]:
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
            _check_partition(expression, columns)
            recorded["partition_by"] = expression.sql("bigquery")
        elif isinstance(prop, exp.ClusterProperty):
            names = [e.name for e in prop.expressions]
            if len(names) > _MAX_CLUSTER_COLUMNS:
                raise errors.invalid(f"Too many clustering columns: {len(names)}, maximum is 4")
            _check_columns(names, columns, "CLUSTER BY")
            for name in names:
                kind = _type_of(name, columns)
                if kind is not None and kind not in _CLUSTER_TYPES:
                    raise errors.invalid(
                        f"Field {name} is of type {kind} which is not supported for clustering"
                    )
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
        if tree.find(exp.MaterializedProperty):
            raise CrucibleUnsupportedError("CREATE MATERIALIZED VIEW")
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
        columns: Columns = {
            n.lower(): t for n, t in zip(translation.names, translation.types, strict=True)
        }
        statement_type = "CREATE_TABLE_AS_SELECT"
    elif isinstance(target, exp.Schema):
        sql = f"{head} {full} ({', '.join(column_def(c) for c in target.expressions)})"
        columns = {c.name.lower(): c.args.get("kind") for c in target.expressions}
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
    properties = table_properties(tree, dict.fromkeys(n.lower() for n in translation.names))

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
    kind = (tree.args.get("kind") or "").upper()
    if kind not in ("TABLE", "VIEW"):
        raise CrucibleUnsupportedError(f"ALTER {tree.args.get('kind')}")
    table = tree.this
    assert isinstance(table, exp.Table)
    info = metadata.find_table(ctx, metadata.name_of(table, ctx))
    full = qualified(info.catalog, info.schema, info.name)
    statements: list[str] = []
    options: dict[str, Any] = {}
    actions: list[exp.Expr] = tree.args.get("actions") or []
    for action in actions:
        if kind == "VIEW" and not isinstance(action, exp.AlterSet):
            raise CrucibleUnsupportedError(f"ALTER VIEW action {action.sql('bigquery')}")
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
            # Labels given in SET OPTIONS replace the whole map; NULL clears an option.
            kept = {k: v for k, v in merged.items() if v is not None}
            c.catalog.set_properties(
                info.catalog, info.schema, info.name, {**properties, "options": kept}
            )

    return _command("ALTER_TABLE", run)


# SQLGlot has no node for ALTER COLUMN ... SET OPTIONS and parses it as a Command.
_ALTER_COLUMN_OPTIONS = re.compile(
    r"TABLE\s+(?P<table>.+?)\s+ALTER\s+COLUMN\s+(?P<column>\S+)"
    r"\s+SET\s+OPTIONS\s*(?P<options>\(.*\))",
    re.IGNORECASE | re.DOTALL,
)


def plan_alter_column(text: str, ctx: ExecContext) -> Plan:
    """ALTER TABLE t ALTER COLUMN c SET OPTIONS (description = ...): the column description."""
    match = _ALTER_COLUMN_OPTIONS.fullmatch(text.strip())
    if match is None:
        raise CrucibleUnsupportedError("ALTER statement")
    try:
        table = sqlglot.parse_one(match["table"], into=exp.Table, read="bigquery")
        alter = sqlglot.parse_one(f"ALTER TABLE t SET OPTIONS {match['options']}", read="bigquery")
    except ParseError as exc:
        raise errors.invalid(f"Syntax error: {exc}") from exc
    info = metadata.find_table(ctx, metadata.name_of(table, ctx))
    column = match["column"].strip("`")
    _check_columns([column], dict.fromkeys(c.name.lower() for c in info.columns), "ALTER COLUMN")
    stored = next(c.name for c in info.columns if c.name.lower() == column.lower())
    options = _options(list(alter.find_all(exp.Property)))
    if set(options) - {"description"}:
        raise CrucibleUnsupportedError(f"ALTER COLUMN option {sorted(set(options))[0]}")

    def run(c: ExecContext) -> None:
        properties = c.catalog.properties(info.catalog, info.schema, info.name)
        descriptions: dict[str, str] = dict(properties.get("field_descriptions", {}))
        if "description" in options:
            descriptions.pop(stored, None)
            if options["description"] is not None:
                descriptions[stored] = options["description"]
        c.catalog.set_properties(
            info.catalog,
            info.schema,
            info.name,
            {**properties, "field_descriptions": descriptions},
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
