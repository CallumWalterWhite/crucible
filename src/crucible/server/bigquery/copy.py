"""Copy jobs (`jobs.insert` with `configuration.copy`; `client.copy_table`).

dbt-bigquery's `insert_overwrite` with `copy_partitions: true` copies one partition at a time,
`tmp$20240101` to `target$20240101` with WRITE_TRUNCATE (connections.py copy_bq_table, macros
insert_overwrite.sql bq_copy_partitions). A `$<partition>` decorator names one partition of a
time- or range-partitioned table: `%Y%m%d` (DAY), `%Y%m%d%H` (HOUR), `%Y%m` (MONTH), `%Y` (YEAR),
or a range start (https://cloud.google.com/bigquery/docs/managing-partitioned-table-data).
Writing into a decorator replaces that partition only. Snapshots, clones, `__NULL__` and
`__UNPARTITIONED__` partitions, and copies Crucible cannot match exactly are refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from crucible.dialects.bigquery import ddl, metadata, types
from crucible.engine import (
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    TableInfo,
)
from crucible.engine.catalog import qualified

_KEYS = frozenset(
    {"sourceTables", "sourceTable", "destinationTable", "writeDisposition", "createDisposition",
     "operationType"}
)  # fmt: skip
_FORMATS = {"DAY": "%Y%m%d", "HOUR": "%Y%m%d%H", "MONTH": "%Y%m", "YEAR": "%Y"}
_LENGTHS = {"DAY": 8, "HOUR": 10, "MONTH": 6, "YEAR": 4}


@dataclass(frozen=True)
class _Ref:
    project: str
    dataset: str
    table: str
    partition: str | None


def _ref(raw: Any, default_project: str) -> _Ref:
    ref = cast("dict[str, Any]", raw or {})
    dataset, table = ref.get("datasetId"), str(ref.get("tableId") or "")
    if not dataset or not table:
        raise CrucibleError(ErrorKind.INVALID_QUERY, "Copy job needs datasetId and tableId")
    name, _, partition = table.partition("$")
    return _Ref(str(ref.get("projectId") or default_project), str(dataset), name, partition or None)


def run_copy(ctx: ExecContext, config: dict[str, Any], project: str) -> int:
    """Copy the source table(s) into the destination; returns the rows copied."""
    for key in config:
        if key not in _KEYS:
            raise CrucibleUnsupportedError(f"copy job configuration field {key}")
    if str(config.get("operationType") or "COPY").upper() != "COPY":
        raise CrucibleUnsupportedError(f"copy job operationType {config['operationType']}")
    raw_sources = config.get("sourceTables") or (
        [config["sourceTable"]] if config.get("sourceTable") else []
    )
    if len(raw_sources) != 1:
        raise CrucibleUnsupportedError("a copy job with other than one source table")
    source = _ref(raw_sources[0], project)
    dest = _ref(config.get("destinationTable"), project)
    write = str(config.get("writeDisposition") or "WRITE_EMPTY").upper()
    create = str(config.get("createDisposition") or "CREATE_IF_NEEDED").upper()
    if write not in ("WRITE_TRUNCATE", "WRITE_APPEND", "WRITE_EMPTY"):
        raise CrucibleUnsupportedError(f"copy job writeDisposition {write}")
    if create not in ("CREATE_IF_NEEDED", "CREATE_NEVER"):
        raise CrucibleUnsupportedError(f"copy job createDisposition {create}")

    src = metadata.find_table(ctx, metadata.Name(source.project, source.dataset, source.table))
    if src.kind != "table":
        raise CrucibleUnsupportedError("a copy job from a view")
    select = f"SELECT * FROM {qualified(src.catalog, src.schema, src.name)}"
    if source.partition is not None:
        select += f" WHERE {_in_partition(src, source.partition)}"

    name = metadata.Name(dest.project, dest.dataset, dest.table)
    metadata.check_new_table(ctx, name)
    target = ctx.catalog.table(dest.project, dest.dataset, dest.table)
    full = qualified(dest.project, dest.dataset, dest.table)
    if target is None:
        if create == "CREATE_NEVER":
            raise CrucibleError(
                ErrorKind.NOT_FOUND, f"Not found: Table {dest.project}:{dest.dataset}.{dest.table}"
            )
        if dest.partition is not None:
            raise CrucibleUnsupportedError("a copy into a partition of a table that does not exist")
        return _replace(ctx, full, select, src, dest)
    if target.kind != "table":
        raise CrucibleUnsupportedError("a copy job into a view")
    if [(c.name, types.type_name(c.type)) for c in target.columns] != [
        (c.name, types.type_name(c.type)) for c in src.columns
    ]:
        raise CrucibleUnsupportedError("a copy between tables whose schemas differ")
    if dest.partition is not None:
        if dest.partition != source.partition:
            raise CrucibleUnsupportedError("a copy into a different partition than the source's")
        where = _in_partition(target, dest.partition)
        if write == "WRITE_EMPTY" and _count(ctx, f"SELECT * FROM {full} WHERE {where}"):
            raise CrucibleUnsupportedError("WRITE_EMPTY copy into a partition that is not empty")
        if write == "WRITE_TRUNCATE":
            # One transaction (one attached database): the partition is replaced, or untouched.
            copied = _count(ctx, select)
            ctx.run_sql("BEGIN TRANSACTION", returns_rows=False)
            try:
                ctx.run_sql(f"DELETE FROM {full} WHERE {where}", returns_rows=False)
                ctx.run_sql(f"INSERT INTO {full} {select}", returns_rows=False)
            except BaseException:
                ctx.run_sql("ROLLBACK", returns_rows=False)
                raise
            ctx.run_sql("COMMIT", returns_rows=False)
            return copied
    elif write == "WRITE_TRUNCATE":
        return _replace(ctx, full, select, src, dest)
    elif write == "WRITE_EMPTY" and _count(ctx, f"SELECT * FROM {full}"):
        raise CrucibleUnsupportedError("WRITE_EMPTY copy into a table that is not empty")
    copied = _count(ctx, select)
    ctx.run_sql(f"INSERT INTO {full} {select}", returns_rows=False)
    return copied


def _replace(ctx: ExecContext, full: str, select: str, src: TableInfo, dest: _Ref) -> int:
    """The destination becomes a copy of the source: rows, schema, partitioning, clustering."""
    ctx.run_sql(f"CREATE OR REPLACE TABLE {full} AS {select}", returns_rows=False)
    props = {
        k: v for k, v in src.properties.items() if k in ("partition_by", "cluster_by", "options")
    }
    ctx.catalog.set_properties(dest.project, dest.dataset, dest.table, props)
    return _count(ctx, f"SELECT * FROM {full}")


def _count(ctx: ExecContext, select: str) -> int:
    return int(ctx.run_sql(f"SELECT count(*) FROM ({select})").rows[0][0])


def _in_partition(info: TableInfo, partition: str) -> str:
    """A DuckDB predicate: the row is in the named partition of `info`."""
    if partition.startswith("__"):
        raise CrucibleUnsupportedError(f"the {partition} partition")
    spec = ddl.rest_partitioning(info.properties)
    if "timePartitioning" in spec:
        time = spec["timePartitioning"]
        unit, field = str(time["type"]), qualified(str(time["field"]))
        fmt = _FORMATS.get(unit)
        if fmt is None or len(partition) != _LENGTHS[unit]:
            raise CrucibleUnsupportedError(
                f"partition decorator ${partition} for {unit} partitioning"
            )
        return f"strftime({field}, '{fmt}') = '{_digits(partition)}'"
    if "rangePartitioning" in spec:
        ranged = spec["rangePartitioning"]
        field = qualified(str(ranged["field"]))
        start, end, step = (int(ranged["range"][k]) for k in ("start", "end", "interval"))
        low = int(_digits(partition, signed=True))
        if low < start or low >= end or (low - start) % step:
            raise CrucibleUnsupportedError(f"partition decorator ${partition}: not a range start")
        return f"{field} >= {low} AND {field} < {min(low + step, end)}"
    raise CrucibleUnsupportedError("a partition decorator on a table that is not partitioned")


def _digits(text: str, *, signed: bool = False) -> str:
    body = text[1:] if signed and text.startswith("-") else text
    if not body.isdigit():
        raise CrucibleUnsupportedError(f"partition decorator ${text}")
    return text
