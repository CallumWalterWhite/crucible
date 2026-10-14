"""Canonical type model: SQLGlot DataType in the DuckDB dialect.

The engine only knows DuckDB types. Each dialect maps them to warehouse types
(dialects/<d>/types.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp


@dataclass(frozen=True)
class Column:
    name: str
    type: exp.DataType


def parse_type(duckdb_type: str) -> exp.DataType:
    """Parse a DuckDB type string, e.g. 'STRUCT(a BIGINT, b VARCHAR[])'."""
    return exp.DataType.build(duckdb_type, dialect="duckdb")


def type_sql(data_type: exp.DataType) -> str:
    return data_type.sql(dialect="duckdb")
