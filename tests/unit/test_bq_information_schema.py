"""INFORMATION_SCHEMA views and __TABLES__, as dbt-bigquery's catalog.sql reads them."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleUnsupportedError
from tests.unit.bigquery import DATASET, PROJECT, values


@pytest.fixture
def tables(bq):
    bq(
        "CREATE TABLE t (k INT64, s STRING, r STRUCT<x INT64, ys ARRAY<STRUCT<z STRING>>>) "
        "PARTITION BY RANGE_BUCKET(k, GENERATE_ARRAY(0, 10, 1)) CLUSTER BY s "
        "OPTIONS (description = 'Say \"hi\"', labels = [('team', 'data')])"
    )
    bq("INSERT INTO t (k, s) VALUES (1, 'a'), (2, 'b')")
    bq("CREATE VIEW v AS SELECT k FROM t")
    return bq


def test_tables_lists_tables_and_views_with_row_counts(tables):
    sql = f"SELECT table_id, row_count, type FROM `{PROJECT}`.`{DATASET}`.__TABLES__ ORDER BY 1"
    assert values(tables(sql)) == [["t", 2, 1], ["v", 0, 2]]


def test_table_options_lists_description_and_labels(tables):
    sql = (
        f"SELECT option_name, option_type, option_value FROM {DATASET}.INFORMATION_SCHEMA."
        "TABLE_OPTIONS WHERE table_name = 't' ORDER BY 1"
    )
    assert values(tables(sql)) == [
        ["description", "STRING", '"Say \\"hi\\""'],
        ["labels", "ARRAY<STRUCT<STRING, STRING>>", '[STRUCT("team", "data")]'],
    ]


def test_columns_report_position_type_partitioning_and_clustering(tables):
    sql = (
        "SELECT table_name, column_name, ordinal_position, is_nullable, data_type, "
        f"is_partitioning_column, clustering_ordinal_position FROM {DATASET}.INFORMATION_SCHEMA."
        "COLUMNS ORDER BY 1, 3"
    )
    assert values(tables(sql)) == [
        ["t", "k", 1, "YES", "INT64", "YES", None],
        ["t", "s", 2, "YES", "STRING", "NO", 1],
        ["t", "r", 3, "YES", "STRUCT<x INT64, ys ARRAY<STRUCT<z STRING>>>", "NO", None],
        ["v", "k", 1, "YES", "INT64", "NO", None],
    ]


def test_column_field_paths_include_nested_fields(tables):
    sql = (
        f"SELECT field_path, data_type FROM {DATASET}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS "
        "WHERE table_name = 't' ORDER BY 1"
    )
    assert [r[0] for r in values(tables(sql))] == ["k", "r", "r.x", "r.ys", "r.ys.z", "s"]


def test_the_region_form_covers_the_project_datasets_in_that_location(tables):
    sql = (
        f"SELECT DISTINCT table_name FROM `{PROJECT}`.`region-us`.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE table_schema = '{DATASET}' ORDER BY 1"
    )
    assert values(tables(sql)) == [["t"], ["v"]]


def test_materialized_views_is_empty(tables):
    sql = f"SELECT COUNT(*) FROM {DATASET}.INFORMATION_SCHEMA.MATERIALIZED_VIEWS"
    assert values(tables(sql)) == [[0]]


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT * FROM {DATASET}.INFORMATION_SCHEMA.COLUMNS",
        f"SELECT c.* FROM {DATASET}.INFORMATION_SCHEMA.COLUMNS AS c",
        f"SELECT is_hidden FROM {DATASET}.INFORMATION_SCHEMA.COLUMNS",
        f"SELECT partition_id FROM {DATASET}.INFORMATION_SCHEMA.PARTITIONS",
        f"SELECT * FROM `{PROJECT}`.`region-us`.__TABLES__",
        "SELECT table_name FROM INFORMATION_SCHEMA.COLUMNS",
    ],
)
def test_other_views_columns_and_forms_are_unsupported(tables, sql):
    with pytest.raises(CrucibleUnsupportedError):
        tables(sql)


def test_an_option_crucible_cannot_render_is_unsupported(bq):
    bq("CREATE TABLE o (a INT64) OPTIONS (friendly_name = 'x')")
    with pytest.raises(CrucibleUnsupportedError, match="friendly_name"):
        bq(f"SELECT option_name FROM {DATASET}.INFORMATION_SCHEMA.TABLE_OPTIONS")
