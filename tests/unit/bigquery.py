"""Shared helper for BigQuery dialect tests: run SQL through the real pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from crucible.dialects.bigquery.dialect import DIALECT
from crucible.engine import Engine, Pipeline, Result

PROJECT, DATASET = "test-project", "ds"
Run = Callable[[str], Result]


def bigquery_runner() -> Iterator[Run]:
    """Yields run(sql) -> the last statement's Result, in a fresh engine with dataset `ds`."""
    engine = Engine()
    session = DIALECT.new_session(PROJECT, DATASET)
    with engine.cursor(session) as cursor:
        engine.catalog(cursor).ensure_schema(PROJECT, DATASET)
    pipeline = Pipeline(engine, DIALECT)
    yield lambda sql: pipeline.execute(session, sql)[-1]
    engine.close()


def table_properties(engine: Engine, table: str) -> dict[str, object]:
    """The properties Crucible recorded for `ds.<table>` (partitioning, clustering, options)."""
    with engine.cursor(DIALECT.new_session()) as cursor:
        return engine.catalog(cursor).properties(PROJECT, DATASET, table)


def column_types(result: Result) -> list[tuple[str, str]]:
    return [(c.name, DIALECT.warehouse_type(c.type)) for c in result.columns]


def values(result: Result) -> list[list[object]]:
    """Rows as canonical values (ADR 0014), as the corpus compares them."""
    types = [DIALECT.warehouse_type(c.type) for c in result.columns]
    return [
        [DIALECT.canonical_value(v, t) for v, t in zip(row, types, strict=True)]
        for row in result.rows
    ]
