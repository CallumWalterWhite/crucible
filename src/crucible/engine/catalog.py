"""Namespace mapping and metadata store (ADR 0002).

Warehouse namespaces map onto ATTACHed DuckDB catalogs and their schemas. Names are passed
through exactly as the dialect resolved them; DuckDB matches them case-insensitively, so each
dialect applies its own case rules before calling in (dialects/<d>/metadata.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import duckdb

from crucible.engine.errors import CrucibleError, ErrorKind, from_duckdb
from crucible.engine.types import Column, parse_type

if TYPE_CHECKING:
    from crucible.engine.engine import Engine

META = "__crucible__"
# DuckDB's own catalogs plus the metadata store; a warehouse namespace cannot use them.
RESERVED_CATALOGS = frozenset({"memory", "system", "temp", META})


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def qualified(*parts: str) -> str:
    return ".".join(quote(p) for p in parts)


@dataclass(frozen=True)
class TableInfo:
    catalog: str
    schema: str
    name: str
    kind: str  # "table" | "view"
    columns: list[Column]
    properties: dict[str, Any] = field(default_factory=dict[str, Any])


class Catalog:
    """Catalog operations bound to one request's cursor, so they see its open transaction."""

    def __init__(self, engine: Engine, cursor: duckdb.DuckDBPyConnection) -> None:
        self._engine = engine
        self._cursor = cursor

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        try:
            return self._cursor.execute(sql, params).fetchall()
        except duckdb.Error as exc:
            raise from_duckdb(exc) from exc

    # --- catalogs --------------------------------------------------------------------------
    def catalog_exists(self, name: str) -> bool:
        return name in self._engine.attached_catalogs()

    def ensure_catalog(self, name: str) -> None:
        if name.lower() in RESERVED_CATALOGS:
            raise CrucibleError(ErrorKind.INVALID_QUERY, f"Catalog name is reserved: {name}")
        self._engine.attach_catalog(name)

    def catalogs(self) -> list[str]:
        return sorted(self._engine.attached_catalogs())

    # --- schemas ---------------------------------------------------------------------------
    def schemas(self, catalog: str) -> list[str]:
        if not self.catalog_exists(catalog):
            return []
        rows = self._rows(
            "select schema_name from duckdb_schemas() where database_name = ? and not internal",
            [catalog],
        )
        return sorted(r[0] for r in rows)

    def schema_exists(self, catalog: str, schema: str) -> bool:
        return any(s.lower() == schema.lower() for s in self.schemas(catalog))

    def ensure_schema(self, catalog: str, schema: str) -> None:
        self.ensure_catalog(catalog)
        self._rows(f"create schema if not exists {qualified(catalog, schema)}")

    def drop_schema(self, catalog: str, schema: str, *, cascade: bool) -> None:
        mode = "cascade" if cascade else "restrict"
        self._rows(f"drop schema {qualified(catalog, schema)} {mode}")
        self._rows(
            f"delete from {META}.main.objects where catalog = ? and lower(schema) = lower(?)",
            [catalog, schema],
        )

    # --- tables and views ------------------------------------------------------------------
    def tables(self, catalog: str, schema: str) -> list[TableInfo]:
        if not self.catalog_exists(catalog):
            return []
        rows = self._rows(
            "select table_name, 'table' from duckdb_tables() "
            "where database_name = ? and lower(schema_name) = lower(?) and not internal "
            "union all select view_name, 'view' from duckdb_views() "
            "where database_name = ? and lower(schema_name) = lower(?) and not internal "
            "order by 1",
            [catalog, schema, catalog, schema],
        )
        return [t for name, _ in rows if (t := self.table(catalog, schema, name)) is not None]

    def table(self, catalog: str, schema: str, name: str) -> TableInfo | None:
        """The table or view with its stored name, columns and properties; None if absent."""
        if not self.catalog_exists(catalog):
            return None
        rows = self._rows(
            "select c.schema_name, c.table_name, c.column_name, c.data_type, "
            "  exists(select 1 from duckdb_views() v where v.database_name = c.database_name "
            "    and v.schema_name = c.schema_name and v.view_name = c.table_name) "
            "from duckdb_columns() c where c.database_name = ? "
            "and lower(c.schema_name) = lower(?) and lower(c.table_name) = lower(?) "
            "order by c.column_index",
            [catalog, schema, name],
        )
        if not rows:
            return None
        stored_schema, stored_name = rows[0][0], rows[0][1]
        return TableInfo(
            catalog=catalog,
            schema=stored_schema,
            name=stored_name,
            kind="view" if rows[0][4] else "table",
            columns=[Column(r[2], parse_type(r[3])) for r in rows],
            properties=self.properties(catalog, stored_schema, stored_name),
        )

    # --- metadata the warehouse has and DuckDB does not (partitioning, labels, ...) ---------
    def properties(self, catalog: str, schema: str, name: str) -> dict[str, Any]:
        rows = self._rows(
            f"select properties from {META}.main.objects "
            "where catalog = ? and lower(schema) = lower(?) and lower(name) = lower(?)",
            [catalog, schema, name],
        )
        return json.loads(rows[0][0]) if rows else {}

    def set_properties(self, catalog: str, schema: str, name: str, props: dict[str, Any]) -> None:
        """Replace an object's properties. Dialects call this on every CREATE and DROP."""
        self.clear_properties(catalog, schema, name)
        self._rows(
            f"insert into {META}.main.objects values (?, ?, ?, ?)",
            [catalog, schema, name, json.dumps(props, sort_keys=True)],
        )

    def clear_properties(self, catalog: str, schema: str, name: str) -> None:
        self._rows(
            f"delete from {META}.main.objects "
            "where catalog = ? and lower(schema) = lower(?) and lower(name) = lower(?)",
            [catalog, schema, name],
        )
