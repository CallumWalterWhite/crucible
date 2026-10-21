"""BigQuery translation backbone: types, literal typing, output names and types, CREATE/INSERT."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlglot import exp

from crucible.dialects.bigquery import types
from crucible.dialects.bigquery.dialect import DIALECT
from crucible.engine import (
    CrucibleError,
    CrucibleUnsupportedError,
    Engine,
    ErrorKind,
    Pipeline,
    Result,
)

PROJECT, DATASET = "test-project", "ds"


@pytest.fixture
def run():
    engine = Engine()
    session = DIALECT.new_session(PROJECT, DATASET)
    with engine.cursor(session) as cursor:
        engine.catalog(cursor).ensure_schema(PROJECT, DATASET)
    pipeline = Pipeline(engine, DIALECT)

    def execute(sql: str) -> Result:
        return pipeline.execute(session, sql)[-1]

    yield execute
    engine.close()


def column_types(result: Result) -> list[tuple[str, str]]:
    return [(c.name, DIALECT.warehouse_type(c.type)) for c in result.columns]


# --- types --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("bigquery", "duckdb"),
    [
        ("INT64", "BIGINT"),
        ("FLOAT64", "DOUBLE"),
        ("NUMERIC", "DECIMAL(38, 9)"),
        ("NUMERIC(10, 2)", "DECIMAL(10, 2)"),
        ("STRING", "TEXT"),
        ("BYTES", "BLOB"),
        ("DATETIME", "TIMESTAMP"),
        ("TIMESTAMP", "TIMESTAMPTZ"),
        ("ARRAY<STRUCT<a INT64, b ARRAY<DATE>>>", 'STRUCT("a" BIGINT, "b" DATE[])[]'),
    ],
)
def test_storage_type(bigquery, duckdb):
    assert types.storage_type(types.parse(bigquery)).sql("duckdb") == duckdb


@pytest.mark.parametrize("bigquery", ["BIGNUMERIC", "GEOGRAPHY"])
def test_storage_type_unsupported(bigquery):
    with pytest.raises(CrucibleUnsupportedError):
        types.storage_type(types.parse(bigquery))


@pytest.mark.parametrize(
    ("duckdb", "name"),
    [
        ("BIGINT", "INT64"),
        ("DECIMAL(38,9)", "NUMERIC"),
        ("TIMESTAMP", "DATETIME"),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP"),
        ("STRUCT(a BIGINT, b VARCHAR[])", "STRUCT<a INT64, b ARRAY<STRING>>"),
    ],
)
def test_type_name(duckdb, name):
    assert types.type_name(exp.DataType.build(duckdb, dialect="duckdb")) == name


@pytest.mark.parametrize("duckdb", ["HUGEINT", "INTEGER", "DECIMAL(18,3)", "UBIGINT"])
def test_type_name_refuses_types_bigquery_lacks(duckdb):
    with pytest.raises(CrucibleUnsupportedError):
        types.type_name(exp.DataType.build(duckdb, dialect="duckdb"))


@pytest.mark.parametrize(
    ("value", "type_name", "canonical"),
    [
        (Decimal("2.350000000"), "NUMERIC", "2.35"),
        (Decimal("100.000000000"), "NUMERIC", "100"),
        (Decimal("-0E-9"), "NUMERIC", "0"),
        (float("nan"), "FLOAT64", "NaN"),
        (float("-inf"), "FLOAT64", "-Infinity"),
        (b"\x01\xff", "BYTES", "Af8="),
        (date(2024, 2, 29), "DATE", "2024-02-29"),
        (datetime(2024, 1, 2, 3, 4, 5), "DATETIME", "2024-01-02T03:04:05"),
        (datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=UTC), "TIMESTAMP", "2024-01-02T03:04:05.000006Z"),
        (
            {"a": 1, "b": [Decimal("1.5")]},
            "STRUCT<a INT64, b ARRAY<NUMERIC>>",
            {"a": 1, "b": ["1.5"]},
        ),
        (None, "INT64", None),
    ],
)
def test_canonical_value(value, type_name, canonical):
    assert types.canonical_value(value, type_name) == canonical


@pytest.mark.parametrize(
    ("source", "target", "literal", "ok"),
    [
        ("INT64", "FLOAT64", False, True),
        ("INT64", "NUMERIC", False, True),
        ("FLOAT64", "INT64", False, False),
        ("STRING", "DATE", True, True),
        ("STRING", "DATE", False, False),
        ("DATE", "DATETIME", False, True),
        ("STRUCT<a INT64>", "STRUCT<x FLOAT64>", False, True),
        ("ARRAY<INT64>", "ARRAY<FLOAT64>", False, False),
        ("ARRAY<STRING>", "ARRAY<INT64>", False, False),
    ],
)
def test_coercion(source, target, literal, ok):
    assert types.coerces(types.parse(source), types.parse(target), literal_string=literal) is ok


# --- queries: output types come from the BigQuery AST (ADR 0014) -------------------------------
def test_sum_int64_is_int64_not_hugeint(run):
    run("CREATE TABLE t (a INT64)")
    run("INSERT INTO t VALUES (1), (2)")
    result = run("SELECT SUM(a) AS s FROM t")
    assert column_types(result) == [("s", "INT64")]
    assert result.rows == [(3,)]


def test_sum_overflow_raises_like_bigquery(run):
    run("CREATE TABLE t (a INT64)")
    run("INSERT INTO t VALUES (9223372036854775807), (1)")
    with pytest.raises(CrucibleError) as info:
        run("SELECT SUM(a) AS s FROM t")
    assert info.value.kind is ErrorKind.RUNTIME


def test_literals_follow_bigquery_types(run):
    result = run("SELECT 2147483647 + 1 AS i, 2.5 AS f, ROUND(2.5) AS r, COALESCE(NULL, 1.5) AS c")
    assert column_types(result) == [
        ("i", "INT64"),
        ("f", "FLOAT64"),
        ("r", "FLOAT64"),
        ("c", "FLOAT64"),
    ]
    assert result.rows == [(2147483648, 2.5, 3.0, 1.5)]


def test_literal_positions_are_not_cast(run):
    run("CREATE TABLE t (a INT64, b STRING)")
    run("INSERT INTO t VALUES (1, 'x'), (1, 'y'), (2, 'z')")
    result = run("SELECT a, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1")
    assert result.rows == [(1, 2)]
    result = run("SELECT ROUND(1.2345, 2) AS r, REGEXP_EXTRACT('ab12', 'b([0-9])') AS g")
    assert result.rows == [(1.23, "1")]


def test_numeric_is_decimal_38_9(run):
    result = run(
        "SELECT NUMERIC '123456789.123456789' AS n, "
        "CAST('1.000000001' AS NUMERIC) * CAST('2' AS NUMERIC) AS m"
    )
    assert column_types(result) == [("n", "NUMERIC"), ("m", "NUMERIC")]
    assert result.rows == [(Decimal("123456789.123456789"), Decimal("2.000000002"))]


def test_bignumeric_raises_unsupported(run):
    with pytest.raises(CrucibleUnsupportedError):
        run("SELECT CAST('1' AS BIGNUMERIC) AS b")


def test_array_concat_operator_keeps_array_type(run):
    result = run("SELECT [1, 2] || [3] AS v")
    assert column_types(result) == [("v", "ARRAY<INT64>")]
    assert result.rows == [([1, 2, 3],)]


# --- output names ------------------------------------------------------------------------------
def test_output_names_keep_case_and_number_anonymous_columns(run):
    run("CREATE TABLE t (MixedCase INT64)")
    run("INSERT INTO t VALUES (1)")
    result = run("SELECT mixedcase, MIXEDCASE AS Renamed, 1, mixedcase + 1 FROM t")
    assert [c.name for c in result.columns] == ["mixedcase", "Renamed", "f0_", "f1_"]
    assert [c.name for c in run("SELECT * FROM t").columns] == ["MixedCase"]


def test_union_takes_names_and_types_from_annotation(run):
    result = run("SELECT 1 AS a UNION ALL SELECT 2.5")
    assert column_types(result) == [("a", "FLOAT64")]
    assert sorted(result.rows) == [(1.0,), (2.5,)]


# --- names and errors --------------------------------------------------------------------------
def test_table_names_are_case_sensitive(run):
    run("CREATE TABLE CaseTable (a INT64)")
    with pytest.raises(CrucibleError) as info:
        run("SELECT * FROM casetable")
    assert info.value.kind is ErrorKind.NOT_FOUND
    with pytest.raises(CrucibleUnsupportedError):
        run("CREATE TABLE casetable (a INT64)")


def test_unknown_column_is_invalid_query(run):
    run("CREATE TABLE t (a INT64)")
    with pytest.raises(CrucibleError) as info:
        run("SELECT nope FROM t")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert "Unrecognized name: nope" in info.value.message


def test_missing_dataset_is_not_found(run):
    with pytest.raises(CrucibleError) as info:
        run("SELECT * FROM other.t")
    assert info.value.kind is ErrorKind.NOT_FOUND
    assert "Dataset" in info.value.message


def test_syntax_error_has_location(run):
    with pytest.raises(CrucibleError) as info:
        run("SELECT FROM WHERE")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert info.value.location is not None


def test_reserved_word_identifiers_are_quoted(run):
    run("CREATE TABLE t (similar INT64)")
    run("INSERT INTO t VALUES (7)")
    assert run("SELECT similar FROM t").rows == [(7,)]


# --- CREATE / INSERT ---------------------------------------------------------------------------
def test_insert_refuses_float_into_int64(run):
    run("CREATE TABLE t (a INT64)")
    with pytest.raises(CrucibleError) as info:
        run("INSERT INTO t (a) VALUES (1.5)")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert "cannot be inserted into column a" in info.value.message


def test_insert_coerces_like_bigquery(run):
    run("CREATE TABLE t (f FLOAT64, n NUMERIC, d DATE)")
    run("INSERT INTO t VALUES (1, 2, '2024-02-29')")
    result = run("SELECT * FROM t")
    assert column_types(result) == [("f", "FLOAT64"), ("n", "NUMERIC"), ("d", "DATE")]
    assert result.rows == [(1.0, Decimal("2"), date(2024, 2, 29))]


def test_insert_reports_rowcount(run):
    run("CREATE TABLE t (a INT64)")
    result = run("INSERT INTO t SELECT x FROM UNNEST([1, 2, 3]) AS x")
    assert (result.statement_type, result.rowcount) == ("INSERT", 3)


def test_ctas_stores_bigquery_types_and_records_properties(run):
    result = run("CREATE TABLE t AS SELECT 1 AS i, 2.5 AS f, NUMERIC '1.5' AS n, [1] AS a")
    assert result.statement_type == "CREATE_TABLE_AS_SELECT"
    stored = run("SELECT * FROM t")
    assert column_types(stored) == [
        ("i", "INT64"),
        ("f", "FLOAT64"),
        ("n", "NUMERIC"),
        ("a", "ARRAY<INT64>"),
    ]


def test_create_table_options_are_accepted(run):
    # Recorded as properties (checked in test_bq_statements); they do not change results.
    assert (
        run("CREATE TABLE t (a INT64) OPTIONS (description = 'x')").statement_type == "CREATE_TABLE"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM ds.INFORMATION_SCHEMA.TABLES",
        "SELECT * FROM `test-project.ds.t_*`",
        "BEGIN SELECT 1; END",
        "CREATE TEMP TABLE tt (a INT64)",
        "SELECT * FROM ds.t FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()",
        "SELECT ST_GEOGPOINT(1, 2)",
        "SELECT RANGE(DATE '2024-01-01', NULL)",
        "SELECT * FROM ML.PREDICT(MODEL ds.m, (SELECT 1 AS x))",
        "SELECT SHA512('a')",
        "CREATE MATERIALIZED VIEW mv AS SELECT 1 AS a",
    ],
)
def test_out_of_scope_constructs_raise_unsupported(run, sql):
    with pytest.raises(CrucibleUnsupportedError):
        run(sql)


def test_empty_array_literal_coerces_to_any_array_column(run):
    run("CREATE TABLE t (tags ARRAY<STRING>)")
    run("INSERT INTO t VALUES ([]), (['a'])")
    assert sorted(run("SELECT tags FROM t").rows) == [([],), (["a"],)]


def test_empty_array_in_a_set_operation_takes_the_typed_side(run):
    result = run("SELECT [] AS a UNION ALL SELECT ['x']")
    assert column_types(result) == [("a", "ARRAY<STRING>")]
