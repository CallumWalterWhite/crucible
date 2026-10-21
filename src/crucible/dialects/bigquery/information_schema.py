"""The INFORMATION_SCHEMA views and the legacy __TABLES__ table that dbt-bigquery reads.

`dbt docs generate` (dbt-bigquery 1.9 and 1.12, macros/catalog/catalog.sql) reads `__TABLES__`,
MATERIALIZED_VIEWS, TABLE_OPTIONS, COLUMNS and COLUMN_FIELD_PATHS, dataset-qualified. Each is
built here from the catalog as a typed inline relation, before qualification, so the rest of the
query translates as usual. The region form (`region-us`.INFORMATION_SCHEMA.X) covers the
project's datasets in that location. Columns are those of the BigQuery reference pages
(https://cloud.google.com/bigquery/docs/information-schema-intro); a column Crucible cannot fill
exactly is listed as unsupported and refused when a query reads it, as is SELECT * and any other
view.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from crucible.dialects.bigquery import metadata, types
from crucible.engine import CrucibleUnsupportedError, ExecContext, TableInfo
from crucible.engine.catalog import qualified

DEFAULT_LOCATION = "US"  # the server's default dataset location (server/bigquery/jobs.py)
Row = tuple[Any, ...]


@dataclass(frozen=True)
class _View:
    columns: tuple[tuple[str, str], ...]  # (name, BigQuery type) Crucible fills
    unsupported: frozenset[str]  # documented columns it cannot fill exactly
    rows: Callable[[ExecContext, list[TableInfo]], list[Row]]
    region: bool = True  # also offered as `region-<location>`.INFORMATION_SCHEMA.<view>


def _key(info: TableInfo) -> Row:
    return (info.catalog, info.schema, info.name)


def _tables_rows(ctx: ExecContext, infos: list[TableInfo]) -> list[Row]:
    rows: list[Row] = []
    for info in infos:
        count = 0
        if info.kind == "table":
            name = qualified(info.catalog, info.schema, info.name)
            count = int(ctx.run_sql(f"select count(*) from {name}").rows[0][0])
        # type: 1 table, 2 view (dbt catalog.sql). size_bytes is 0: storage size is not emulated.
        rows.append((*_key(info), count, 0, 1 if info.kind == "table" else 2))
    return rows


def _option_rows(ctx: ExecContext, infos: list[TableInfo]) -> list[Row]:
    rows: list[Row] = []
    for info in infos:
        options: dict[str, Any] = info.properties.get("options") or {}
        for name, value in options.items():
            if name.lower() == "description":
                rows.append((*_key(info), "description", "STRING", json.dumps(str(value))))
            elif name.lower() == "labels":
                rows.append(
                    (*_key(info), "labels", "ARRAY<STRUCT<STRING, STRING>>", _labels(str(value)))
                )
            else:
                raise CrucibleUnsupportedError(f"INFORMATION_SCHEMA.TABLE_OPTIONS option {name}")
    return rows


def _labels(text: str) -> str:
    """`[('k', 'v')]` as stored from OPTIONS(labels = ...), in BigQuery's option_value form."""
    node = sqlglot.parse_one(text, read="bigquery")
    pairs: list[str] = []
    for item in node.expressions if isinstance(node, exp.Array) else []:
        parts = item.expressions if isinstance(item, exp.Tuple | exp.Struct) else []
        if len(parts) != 2 or not all(isinstance(p, exp.Literal) for p in parts):
            raise CrucibleUnsupportedError(f"INFORMATION_SCHEMA.TABLE_OPTIONS labels {text}")
        pairs.append(f"STRUCT({json.dumps(parts[0].this)}, {json.dumps(parts[1].this)})")
    return f"[{', '.join(pairs)}]"


def _column_rows(ctx: ExecContext, infos: list[TableInfo]) -> list[Row]:
    rows: list[Row] = []
    for info in infos:
        partition = _partition_column(info)
        cluster_by: list[Any] = info.properties.get("cluster_by") or []
        clustering = [str(c).lower() for c in cluster_by]
        for position, column in enumerate(info.columns, start=1):
            name = column.name.lower()
            rows.append(
                (
                    *_key(info),
                    column.name,
                    position,
                    "YES",  # shortcut: REQUIRED columns are not stored yet; all are nullable
                    types.type_name(column.type),
                    "YES" if name == partition else "NO",
                    clustering.index(name) + 1 if name in clustering else None,
                )
            )
    return rows


def _partition_column(info: TableInfo) -> str | None:
    text = info.properties.get("partition_by")
    if not isinstance(text, str):
        return None
    node = sqlglot.parse_one(text, read="bigquery")
    column = node if isinstance(node, exp.Column) else node.find(exp.Column)
    return column.name.lower() if column is not None else None


def _field_path_rows(ctx: ExecContext, infos: list[TableInfo]) -> list[Row]:
    rows: list[Row] = []
    for info in infos:
        described: dict[str, Any] = info.properties.get("field_descriptions") or {}
        for column in info.columns:
            for path, data_type in _paths(column.name, column.type):
                rows.append(
                    (
                        *_key(info),
                        column.name,
                        path,
                        types.type_name(data_type),
                        described.get(path),
                    )
                )
    return rows


def _paths(path: str, data_type: exp.DataType) -> list[tuple[str, exp.DataType]]:
    """A column and, for STRUCT fields (also inside ARRAY), each nested field path."""
    found = [(path, data_type)]
    inner = data_type
    if inner.this == exp.DType.ARRAY and inner.expressions:
        inner = inner.expressions[0]
    if inner.this == exp.DType.STRUCT:
        for field in inner.expressions:
            assert isinstance(field, exp.ColumnDef)
            kind = field.args.get("kind")
            assert isinstance(kind, exp.DataType)
            found += _paths(f"{path}.{field.name}", kind)
    return found


_S, _I, _T = "STRING", "INT64", "TIMESTAMP"
_KEYS = (("table_catalog", _S), ("table_schema", _S), ("table_name", _S))
VIEWS: dict[str, _View] = {
    "__TABLES__": _View(
        (("project_id", _S), ("dataset_id", _S), ("table_id", _S), ("row_count", _I),
         ("size_bytes", _I), ("type", _I)),
        frozenset({"creation_time", "last_modified_time"}),
        _tables_rows,
        region=False,
    ),
    "INFORMATION_SCHEMA.MATERIALIZED_VIEWS": _View(
        (*_KEYS, ("last_refresh_time", _T), ("refresh_watermark", _T)),
        frozenset({"last_refresh_status"}),
        lambda ctx, infos: [],  # CREATE MATERIALIZED VIEW is refused, so there are none
    ),
    "INFORMATION_SCHEMA.TABLE_OPTIONS": _View(
        (*_KEYS, ("option_name", _S), ("option_type", _S), ("option_value", _S)),
        frozenset(),
        _option_rows,
    ),
    "INFORMATION_SCHEMA.COLUMNS": _View(
        (*_KEYS, ("column_name", _S), ("ordinal_position", _I), ("is_nullable", _S),
         ("data_type", _S), ("is_partitioning_column", _S), ("clustering_ordinal_position", _I)),
        frozenset({
            "is_generated", "generation_expression", "is_stored", "async_generation_status",
            "is_hidden", "is_updatable", "is_system_defined", "collation_name", "column_default",
            "rounding_mode", "data_policies", "policy_tags", "is_identity", "identity_generation",
            "identity_start", "identity_increment", "identity_maximum", "identity_minimum",
            "identity_cycle",
        }),
        _column_rows,
    ),
    "INFORMATION_SCHEMA.COLUMN_FIELD_PATHS": _View(
        (*_KEYS, ("column_name", _S), ("field_path", _S), ("data_type", _S),
         ("description", _S)),
        frozenset({"collation_name", "rounding_mode", "data_policies", "policy_tags"}),
        _field_path_rows,
    ),
}  # fmt: skip


def _is_virtual(table: exp.Table) -> bool:
    return table.name.upper().startswith("INFORMATION_SCHEMA.") or table.name == "__TABLES__"


def expand(query: exp.Expr, ctx: ExecContext) -> None:
    """Replace each INFORMATION_SCHEMA / __TABLES__ reference in `query` with its rows."""
    used: dict[str, _View] = {}
    for table in [t for t in query.find_all(exp.Table) if _is_virtual(t)]:
        view_name = table.name if table.name == "__TABLES__" else table.name.upper()
        view = VIEWS.get(view_name)
        if view is None:
            raise CrucibleUnsupportedError(view_name)
        alias = table.alias or table.name.split(".")[-1]
        used[alias.lower()] = view
        infos = _tables_in(table, view, ctx)
        table.replace(_relation(view, view.rows(ctx, infos), alias))
    if used:
        _refuse_unsupported_columns(query, used)


def _tables_in(table: exp.Table, view: _View, ctx: ExecContext) -> list[TableInfo]:
    project = table.catalog or ctx.session.default_catalog
    where = table.db
    if not project or not where:
        raise CrucibleUnsupportedError(f"{table.name} without a dataset or region qualifier")
    if where.lower().startswith("region-"):
        if not view.region:
            raise CrucibleUnsupportedError(f"region-qualified {table.name}")
        location = where[len("region-") :].lower()
        datasets = [
            d for d in ctx.catalog.schemas(project) if _location(ctx, project, d) == location
        ]
    else:
        metadata.check_dataset(ctx, project, where)
        datasets = [where]
    return [info for d in datasets for info in ctx.catalog.tables(project, d)]


def _location(ctx: ExecContext, project: str, dataset: str) -> str:
    rest: dict[str, Any] = ctx.catalog.properties(project, dataset, "").get("rest") or {}
    return str(rest.get("location") or DEFAULT_LOCATION).lower()


def _relation(view: _View, rows: list[Row], alias: str) -> exp.Expr:
    struct = ", ".join(f"{name} {kind}" for name, kind in view.columns)
    values = ", ".join("STRUCT(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
    sql = f"SELECT * FROM UNNEST(ARRAY<STRUCT<{struct}>>[{values}])"
    select = sqlglot.parse_one(sql, read="bigquery")
    return exp.Subquery(this=select, alias=exp.TableAlias(this=exp.to_identifier(alias)))


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return exp.Literal.string(str(value)).sql("bigquery")


def _refuse_unsupported_columns(query: exp.Expr, used: dict[str, _View]) -> None:
    unsupported = {c for view in used.values() for c in view.unsupported}
    for column in query.find_all(exp.Column):
        if column.name.lower() in unsupported:
            raise CrucibleUnsupportedError(f"INFORMATION_SCHEMA column {column.name}")
    for star in query.find_all(exp.Star):
        select = star.find_ancestor(exp.Select)
        if select is None:
            continue
        if isinstance(star.parent, exp.Count):
            continue  # COUNT(*)
        joins: list[exp.Expr] = select.args.get("joins") or []
        sources: list[exp.Expr | None] = [select.args.get("from_"), *joins]
        aliases = {
            s.this.alias_or_name.lower()
            for s in sources
            if isinstance(s, exp.From | exp.Join) and isinstance(s.this, exp.Expr)
        }
        parent = star.parent
        named = parent.table.lower() if isinstance(parent, exp.Column) and parent.table else None
        if (named in used) if named else bool(aliases & used.keys()):
            raise CrucibleUnsupportedError("SELECT * over INFORMATION_SCHEMA or __TABLES__")
