"""Only BigQuery table expressions reach DuckDB (statements._refuse_unmodelled, ADR 0006)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError
from tests.unit.bigquery import values


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('/tmp/x.parquet')",
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT * FROM range(3)",
        "SELECT (SELECT COUNT(*) FROM read_csv('/etc/hosts'))",
        "SELECT 1 FROM UNNEST([1]) AS x JOIN read_parquet('/tmp/x.parquet') ON TRUE",
        "CREATE TABLE t AS SELECT * FROM read_csv('/etc/hosts')",
        "CREATE VIEW v AS SELECT * FROM read_csv('/etc/hosts')",
        "INSERT INTO t2 SELECT * FROM read_csv('/etc/hosts')",
        "MERGE t2 USING read_csv('/etc/hosts') AS s ON FALSE WHEN NOT MATCHED THEN INSERT ROW",
        "DECLARE x INT64 DEFAULT (SELECT COUNT(*) FROM read_csv('/etc/hosts')); SELECT x",
        "CREATE TEMP FUNCTION f() AS ((SELECT COUNT(*) FROM read_csv('/etc/hosts'))); SELECT f()",
    ],
)
def test_a_table_function_is_refused_in_every_statement(bq, sql):
    bq("CREATE TABLE t2 (a STRING)")
    with pytest.raises(
        CrucibleUnsupportedError, match=r"^Not supported by Crucible: table function"
    ):
        bq(sql)


@pytest.mark.parametrize("name", ["`/etc/hosts`", "`/tmp/x.parquet`", "ds.`/etc/hosts`", "'x.csv'"])
def test_a_file_path_used_as_a_table_name_is_a_missing_table(bq, name):
    with pytest.raises(CrucibleError, match=r"^Not found: (Table|Dataset)"):
        bq(f"SELECT * FROM {name}")


def test_unnest_is_still_a_table_expression(bq):
    assert values(bq("SELECT x FROM UNNEST([1, 2]) AS x ORDER BY x")) == [[1], [2]]
