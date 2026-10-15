"""Statements dbt emits: CREATE SCHEMA, CTAS with PARTITION BY / CLUSTER BY / OPTIONS, views, MERGE,
ALTER TABLE, DROP, and the leading JSON query comment."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, Engine, ErrorKind
from tests.unit.bigquery import DATASET, PROJECT, values
from tests.unit.bigquery import table_properties as props


def test_create_schema(bq):
    assert bq(f"CREATE SCHEMA IF NOT EXISTS `{PROJECT}`.`other`").statement_type == "CREATE_SCHEMA"
    bq("CREATE SCHEMA IF NOT EXISTS other")
    with pytest.raises(CrucibleError) as info:
        bq("CREATE SCHEMA other")
    assert info.value.kind is ErrorKind.ALREADY_EXISTS
    with pytest.raises(CrucibleUnsupportedError):
        bq("CREATE SCHEMA Other")
    with pytest.raises(CrucibleUnsupportedError):
        bq("CREATE SCHEMA main")
    bq("CREATE TABLE other.t (a INT64)")
    assert values(bq("SELECT COUNT(*) AS n FROM other.t")) == [[0]]


def test_ctas_records_partition_cluster_and_options(bq):
    bq(
        "CREATE OR REPLACE TABLE t PARTITION BY d CLUSTER BY k "
        "OPTIONS (description = 'x', labels = [('team', 'data')]) "
        "AS SELECT DATE '2024-01-01' AS d, 'k1' AS k, 1 AS v"
    )
    assert values(bq("SELECT k, v FROM t")) == [["k1", 1]]
    bq(
        "CREATE OR REPLACE TABLE r PARTITION BY RANGE_BUCKET(n, GENERATE_ARRAY(0, 100, 10)) "
        "AS SELECT 5 AS n"
    )
    bq(
        "CREATE OR REPLACE TABLE m PARTITION BY DATE_TRUNC(d, MONTH) "
        "AS SELECT DATE '2024-01-01' AS d"
    )


def test_partition_and_cluster_columns_must_exist(bq):
    with pytest.raises(CrucibleError) as info:
        bq("CREATE TABLE t PARTITION BY nope AS SELECT 1 AS a")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    with pytest.raises(CrucibleError):
        bq("CREATE TABLE t CLUSTER BY nope AS SELECT 1 AS a")
    with pytest.raises(CrucibleError):
        bq(
            "CREATE TABLE t CLUSTER BY a, b, c, d, e "
            "AS SELECT 1 AS a, 2 AS b, 3 AS c, 4 AS d, 5 AS e"
        )


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t OPTIONS (require_partition_filter = TRUE) AS SELECT 1 AS a",
        "CREATE TABLE t PARTITION BY _PARTITIONDATE AS SELECT 1 AS a",
        "CREATE TEMP TABLE t AS SELECT 1 AS a",
        "CREATE FUNCTION f() AS (1)",
    ],
)
def test_options_that_would_change_results_are_unsupported(bq, sql):
    with pytest.raises(CrucibleUnsupportedError):
        bq(sql)


def test_view_with_options(bq):
    assert (
        bq("CREATE OR REPLACE VIEW v OPTIONS (description = 'x') AS SELECT 1 AS a").statement_type
        == "CREATE_VIEW"
    )
    assert values(bq("SELECT a FROM v")) == [[1]]


@pytest.fixture
def merge_tables(bq):
    bq("CREATE TABLE tgt (k STRING, v INT64)")
    bq("INSERT INTO tgt VALUES ('a', 1), ('b', 2)")
    bq("CREATE TABLE src (k STRING, v INT64)")
    bq("INSERT INTO src VALUES ('a', 10), ('c', 30)")
    return bq


def test_merge_on_false_inserts_every_source_row(merge_tables):
    result = merge_tables(
        "MERGE INTO tgt AS d USING (SELECT * FROM src) AS s ON (FALSE) "
        "WHEN NOT MATCHED THEN INSERT (k, v) VALUES (k, v)"
    )
    assert (result.statement_type, result.rowcount) == ("MERGE", 2)
    assert values(merge_tables("SELECT k, v FROM tgt ORDER BY k, v")) == [
        ["a", 1],
        ["a", 10],
        ["b", 2],
        ["c", 30],
    ]


def test_dbt_keyed_merge(merge_tables):
    merge_tables(
        "MERGE INTO tgt AS DBT_INTERNAL_DEST USING (SELECT k, v FROM src) AS DBT_INTERNAL_SOURCE "
        "ON (DBT_INTERNAL_SOURCE.k = DBT_INTERNAL_DEST.k) "
        "WHEN MATCHED THEN UPDATE SET `v` = DBT_INTERNAL_SOURCE.`v` "
        "WHEN NOT MATCHED THEN INSERT (`k`, `v`) VALUES (`k`, `v`)"
    )
    assert values(merge_tables("SELECT k, v FROM tgt ORDER BY k")) == [
        ["a", 10],
        ["b", 2],
        ["c", 30],
    ]


def test_merge_conditions_and_delete(merge_tables):
    merge_tables(
        "MERGE INTO tgt AS d USING src AS s ON s.k = d.k "
        "WHEN MATCHED AND s.v > 100 THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED BY SOURCE THEN DELETE"
    )
    assert values(merge_tables("SELECT k, v FROM tgt ORDER BY k")) == [["a", 1]]


def test_merge_duplicate_source_match_raises_and_changes_nothing(merge_tables):
    merge_tables("INSERT INTO src VALUES ('a', 11)")
    with pytest.raises(CrucibleError) as info:
        merge_tables(
            "MERGE INTO tgt AS d USING src AS s ON s.k = d.k WHEN MATCHED THEN UPDATE SET v = s.v"
        )
    assert info.value.kind is ErrorKind.RUNTIME
    assert "must match at most one source row" in info.value.message
    assert values(merge_tables("SELECT k, v FROM tgt ORDER BY k")) == [["a", 1], ["b", 2]]


def test_merge_without_matched_clause_allows_duplicates(merge_tables):
    merge_tables("INSERT INTO src VALUES ('c', 31)")
    merge_tables(
        "MERGE INTO tgt AS d USING src AS s ON s.k = d.k "
        "WHEN NOT MATCHED THEN INSERT (k, v) VALUES (k, v)"
    )
    assert values(merge_tables("SELECT COUNT(*) AS n FROM tgt WHERE k = 'c'")) == [[2]]


def test_merge_checks_assignment_types(merge_tables):
    with pytest.raises(CrucibleError) as info:
        merge_tables(
            "MERGE INTO tgt AS d USING src AS s ON FALSE "
            "WHEN NOT MATCHED THEN INSERT (k, v) VALUES (k, 1.5)"
        )
    assert info.value.kind is ErrorKind.INVALID_QUERY


def test_alter_table(bq):
    bq("CREATE TABLE t (a INT64)")
    bq("INSERT INTO t VALUES (1)")
    bq("ALTER TABLE t ADD COLUMN IF NOT EXISTS observed_at TIMESTAMP")
    bq("ALTER TABLE t ADD COLUMN IF NOT EXISTS observed_at TIMESTAMP")
    bq("ALTER TABLE t SET OPTIONS (description = 'y')")
    assert values(bq("SELECT * FROM t")) == [[1, None]]
    with pytest.raises(CrucibleError) as info:
        bq("ALTER TABLE t ADD COLUMN observed_at TIMESTAMP")
    assert info.value.kind is ErrorKind.ALREADY_EXISTS


def test_drop(bq):
    bq("CREATE TABLE t (a INT64)")
    assert bq("DROP TABLE t").statement_type == "DROP_TABLE"
    with pytest.raises(CrucibleError) as info:
        bq("SELECT * FROM t")
    assert info.value.kind is ErrorKind.NOT_FOUND
    bq("DROP TABLE IF EXISTS t")
    with pytest.raises(CrucibleError):
        bq("DROP TABLE t")
    bq("CREATE VIEW v AS SELECT 1 AS a")
    with pytest.raises(CrucibleError):
        bq("DROP TABLE v")
    bq("DROP VIEW v")


def test_leading_json_comment(bq):
    assert values(bq('/* {"app": "dbt", "node_id": "model.x.y"} */\nSELECT 1 AS a')) == [[1]]


def test_properties_are_recorded_replaced_and_cleared():
    from crucible.dialects.bigquery.dialect import DIALECT
    from crucible.engine import Pipeline

    engine = Engine()
    session = DIALECT.new_session(PROJECT, DATASET)
    with engine.cursor(session) as cursor:
        engine.catalog(cursor).ensure_schema(PROJECT, DATASET)
    pipeline = Pipeline(engine, DIALECT)

    def run(sql: str) -> None:
        pipeline.execute(session, sql)

    run(
        "CREATE TABLE t PARTITION BY d CLUSTER BY k OPTIONS (description = 'x') "
        "AS SELECT DATE '2024-01-01' AS d, 'k1' AS k"
    )
    assert props(engine, "t") == {
        "partition_by": "d",
        "cluster_by": ["k"],
        "options": {"description": "x"},
    }
    run("ALTER TABLE t SET OPTIONS (labels = [('a', 'b')])")
    assert props(engine, "t")["options"] == {"description": "x", "labels": "[('a', 'b')]"}
    run("CREATE OR REPLACE TABLE t AS SELECT 1 AS a")
    assert props(engine, "t") == {}
    run("DROP TABLE t")
    assert props(engine, "t") == {}
    engine.close()
