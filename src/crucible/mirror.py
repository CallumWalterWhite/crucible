"""`crucible mirror-schema`: copy table and view schemas, never data, from BigQuery into Crucible.

Reads use the source client and only datasets.get, tables.list and tables.get. Writes go to
Crucible: datasets through the REST API, tables and views through DDL. Crucible refuses partitioning
and clustering on tables.insert but accepts them in CREATE TABLE, so tables are created with DDL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.cloud import bigquery

# google-cloud-bigquery is untyped for pyright strict, so its objects are Any (cf. record_api.py).
Client = Any
Table = Any
SchemaField = Any

# Legacy REST type names to the Standard SQL names that DDL takes.
_SQL_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}


class _Unmirrored(Exception):
    """This object cannot be copied exactly, so it is skipped with a warning."""


@dataclass
class Summary:
    datasets: int = 0
    tables: int = 0
    views: int = 0
    skipped: int = 0


def mirror_schema(
    source: Client,
    target: Client,
    from_project: str,
    to_project: str,
    datasets: list[str],
    warn: Callable[[str], None],
) -> Summary:
    summary = Summary()
    tables: list[tuple[str, Table]] = []
    views: list[tuple[str, Table]] = []
    for dataset in datasets:
        source_dataset = source.get_dataset(f"{from_project}.{dataset}")
        target_dataset = bigquery.Dataset(f"{to_project}.{dataset}")
        target_dataset.description = source_dataset.description
        target_dataset.labels = source_dataset.labels
        target.create_dataset(target_dataset, exists_ok=True)
        summary.datasets += 1
        for item in source.list_tables(source_dataset):
            table = source.get_table(item.reference)
            if table.table_type == "TABLE":
                tables.append((dataset, table))
            elif table.table_type == "VIEW":
                views.append((dataset, table))
            else:
                warn(f"skipped {dataset}.{table.table_id}: {table.table_type} is not mirrored")
                summary.skipped += 1
    # Tables first: a view may read any table of the run, whichever dataset it is in.
    for dataset, table in tables:
        try:
            _create_table(target, to_project, dataset, table)
        except (_Unmirrored, GoogleAPICallError) as exc:
            warn(f"skipped table {dataset}.{table.table_id}: {_reason(exc)}")
            summary.skipped += 1
        else:
            summary.tables += 1
    for dataset, table in views:
        try:
            _create_view(target, to_project, dataset, table)
        except (_Unmirrored, GoogleAPICallError) as exc:
            warn(f"skipped view {dataset}.{table.table_id}: {_reason(exc)}")
            summary.skipped += 1
        else:
            summary.views += 1
    return summary


def _create_table(target: Client, to_project: str, dataset: str, table: Table) -> None:
    ref = f"{to_project}.{dataset}.{table.table_id}"
    if _exists(target, ref):
        raise _Unmirrored("already in Crucible, left as it is")
    _check_nested(table.schema)
    columns = ", ".join(_column(f) for f in table.schema)
    cluster = ""
    if table.clustering_fields:
        cluster = " CLUSTER BY " + ", ".join(f"`{name}`" for name in table.clustering_fields)
    target.query(
        f"CREATE TABLE `{to_project}`.`{dataset}`.`{table.table_id}` ({columns})"
        f"{_partition_by(table)}{cluster}{_options(table)}"
    ).result()
    # DDL takes no column descriptions, so they go in a schema patch (tables.patch).
    if any(f.description for f in table.schema):
        created = target.get_table(ref)
        created.schema = table.schema
        try:
            target.update_table(created, ["schema"])
        except GoogleAPICallError:
            target.delete_table(ref)  # created just above, so nothing is lost
            raise


def _create_view(target: Client, to_project: str, dataset: str, table: Table) -> None:
    if table.view_use_legacy_sql:
        raise _Unmirrored("legacy SQL view")
    # The SQL is sent as the source wrote it. A reference to a project other than --to-project
    # does not resolve, so Crucible refuses the view and it is skipped.
    target.query(
        f"CREATE OR REPLACE VIEW `{to_project}`.`{dataset}`.`{table.table_id}`{_options(table)}"
        f" AS {table.view_query}"
    ).result()


def _exists(target: Client, ref: str) -> bool:
    try:
        target.get_table(ref)
    except NotFound:
        return False
    return True


def _check_nested(fields: list[SchemaField], nested: bool = False) -> None:
    """Crucible cannot store a description or a REQUIRED mode on a nested field."""
    for field in fields:
        if nested and (field.mode == "REQUIRED" or field.description):
            raise _Unmirrored(f"nested field {field.name} has a REQUIRED mode or a description")
        _check_nested(field.fields, nested=True)


def _column(field: SchemaField) -> str:
    not_null = " NOT NULL" if field.mode == "REQUIRED" else ""
    return f"`{field.name}` {_type(field)}{not_null}"


def _type(field: SchemaField) -> str:
    if field.field_type in ("RECORD", "STRUCT"):
        kind = "STRUCT<" + ", ".join(f"`{sub.name}` {_type(sub)}" for sub in field.fields) + ">"
    else:
        kind = _SQL_TYPES.get(field.field_type) or field.field_type
    return f"ARRAY<{kind}>" if field.mode == "REPEATED" else kind


def _partition_by(table: Table) -> str:
    time, ranged = table.time_partitioning, table.range_partitioning
    if time is not None:
        if time.field is None:
            raise _Unmirrored("ingestion-time partitioning")
        column = next(f for f in table.schema if f.name == time.field)
        kind = _SQL_TYPES.get(column.field_type) or column.field_type
        if kind == "DATE" and time.type_ == "DAY":
            return f" PARTITION BY `{time.field}`"
        return f" PARTITION BY {kind}_TRUNC(`{time.field}`, {time.type_})"
    if ranged is not None:
        bounds = ranged.range_
        return (
            f" PARTITION BY RANGE_BUCKET(`{ranged.field}`, "
            f"GENERATE_ARRAY({bounds.start}, {bounds.end}, {bounds.interval}))"
        )
    return ""


def _options(table: Table) -> str:
    options: list[str] = []
    if table.description:
        options.append(f"description={json.dumps(table.description)}")
    if table.labels:
        pairs = ", ".join(f"({json.dumps(k)}, {json.dumps(v)})" for k, v in table.labels.items())
        options.append(f"labels=[{pairs}]")
    if table.require_partition_filter:
        options.append("require_partition_filter=TRUE")  # Crucible refuses this one
    return f" OPTIONS({', '.join(options)})" if options else ""


def _reason(exc: Exception) -> str:
    return str(getattr(exc, "message", exc))  # GoogleAPICallError carries the bare message
