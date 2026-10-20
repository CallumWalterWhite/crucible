"""BigQuery name resolution and identifier case rules (ADR 0002).

Project, dataset and table names are case-sensitive: a lookup must match the stored name exactly.
Column names are case-insensitive. DuckDB matches every name case-insensitively, so this module
compares exact names itself and refuses names DuckDB could not keep apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from crucible.dialects.bigquery import errors
from crucible.engine import CrucibleUnsupportedError, ExecContext, TableInfo


@dataclass(frozen=True)
class Name:
    project: str
    dataset: str
    table: str


def name_of(table: exp.Table, ctx: ExecContext) -> Name:
    """The full name of a table reference, filling in the session's default project and dataset."""
    project = table.catalog or ctx.session.default_catalog
    dataset = table.db or ctx.session.default_schema
    if "INFORMATION_SCHEMA" in table.sql("bigquery").upper():
        raise CrucibleUnsupportedError("INFORMATION_SCHEMA")
    if table.name.endswith("*"):
        raise CrucibleUnsupportedError(f"wildcard table {table.sql('bigquery')}")
    if table.args.get("version"):
        raise CrucibleUnsupportedError("FOR SYSTEM_TIME AS OF")
    if not dataset:
        raise errors.invalid(
            f'Table "{table.name}" must be qualified with a dataset (e.g. dataset.table).'
        )
    if not project:
        raise errors.invalid(f"Table {dataset}.{table.name} needs a project")
    return Name(project, dataset, table.name)


def check_dataset(ctx: ExecContext, project: str, dataset: str) -> None:
    """Raise unless the dataset exists with exactly this name."""
    if dataset.lower() == "main":
        raise CrucibleUnsupportedError("a dataset named main (reserved by DuckDB)")
    if dataset not in ctx.catalog.schemas(project):
        raise errors.dataset_not_found(project, dataset)


def find_table(ctx: ExecContext, name: Name) -> TableInfo:
    check_dataset(ctx, name.project, name.dataset)
    info = ctx.catalog.table(name.project, name.dataset, name.table)
    if info is None or info.name != name.table:
        raise errors.table_not_found(name.project, name.dataset, name.table)
    return info


def check_new_table(ctx: ExecContext, name: Name) -> None:
    """Before CREATE: the dataset exists and no table differs from this name only by case."""
    check_dataset(ctx, name.project, name.dataset)
    info = ctx.catalog.table(name.project, name.dataset, name.table)
    if info is not None and info.name != name.table:
        raise CrucibleUnsupportedError(
            f"table {name.table} next to {info.name}: names that differ only by case"
        )


def cte_names(tree: exp.Expr) -> set[str]:
    return {cte.alias_or_name for cte in tree.find_all(exp.CTE)}


def referenced_tables(tree: exp.Expr) -> list[exp.Table]:
    """Table references that name stored tables (not CTEs, UNNEST or table functions)."""
    ctes = cte_names(tree)
    tables: list[exp.Table] = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        if not table.db and not table.catalog and table.name in ctes:
            continue
        tables.append(table)
    return tables


def qualify_names(tree: exp.Expr, ctx: ExecContext) -> list[TableInfo]:
    """Resolve every stored table reference to its exact full name, in place."""
    infos: list[TableInfo] = []
    for table in referenced_tables(tree):
        info = find_table(ctx, name_of(table, ctx))
        table.set("catalog", exp.to_identifier(info.catalog, quoted=True))
        table.set("db", exp.to_identifier(info.schema, quoted=True))
        table.set("this", exp.to_identifier(info.name, quoted=True))
        infos.append(info)
    return infos
