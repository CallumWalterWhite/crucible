"""SQL user-defined functions (udfs.py): expansion, type checks, persistence and refusals."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import values


def test_a_temp_function_is_expanded_with_typed_arguments(bq):
    sql = (
        "CREATE TEMP FUNCTION f(x INT64, y STRING) RETURNS STRING "
        "AS (CONCAT(y, CAST(x AS STRING))); "
        "SELECT f(1, 'a') AS v"
    )
    assert values(bq(sql)) == [["a1"]]


def test_an_argument_coerces_like_a_bigquery_function_argument(bq):
    assert values(bq("CREATE TEMP FUNCTION f(x FLOAT64) AS (x * 2); SELECT f(1) AS v")) == [[2.0]]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TEMP FUNCTION f(x INT64) AS (x); SELECT f('a')",
        "CREATE TEMP FUNCTION f(x INT64) AS (x); SELECT f(1, 2)",
    ],
)
def test_a_mismatched_call_has_no_matching_signature(bq, sql):
    with pytest.raises(CrucibleError) as info:
        bq(sql)
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert "No matching signature for function f" in info.value.message


def test_a_body_that_does_not_match_returns_is_an_error(bq):
    with pytest.raises(CrucibleError, match="declared to return INT64"):
        bq("CREATE TEMP FUNCTION f(x INT64) RETURNS INT64 AS ('s'); SELECT f(1)")


def test_persistent_functions_are_created_called_nested_and_dropped(bq):
    bq("CREATE OR REPLACE FUNCTION ds.g(x FLOAT64) AS (x * 2)")
    bq("CREATE FUNCTION ds.h(x INT64) AS (ds.g(x) + 1)")
    bq("CREATE TABLE t (a INT64)")
    bq("INSERT INTO t VALUES (1), (2)")
    assert values(bq("SELECT ds.h(3) AS h, SUM(ds.g(a)) AS s FROM t")) == [[7.0, 6.0]]
    with pytest.raises(CrucibleError) as info:
        bq("CREATE FUNCTION ds.g(x FLOAT64) AS (x)")
    assert info.value.kind is ErrorKind.ALREADY_EXISTS
    bq("CREATE FUNCTION IF NOT EXISTS ds.g(x FLOAT64) AS (x)")
    bq("DROP FUNCTION ds.g")
    with pytest.raises(CrucibleError, match=r"Function not found: ds\.g"):
        bq("SELECT ds.g(1.5)")
    with pytest.raises(CrucibleError) as info:
        bq("DROP FUNCTION ds.g")
    assert info.value.kind is ErrorKind.NOT_FOUND
    bq("DROP FUNCTION IF EXISTS ds.g")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE FUNCTION ds.j(x STRING) RETURNS STRING LANGUAGE js AS 'return x;'",
        "CREATE TEMP FUNCTION t(x INT64) RETURNS TABLE<a INT64> AS (SELECT x AS a)",
        "CREATE FUNCTION ds.a(x ANY TYPE) AS (x)",
    ],
)
def test_javascript_table_valued_and_templated_functions_are_unsupported(bq, sql):
    with pytest.raises(CrucibleUnsupportedError):
        bq(sql)
