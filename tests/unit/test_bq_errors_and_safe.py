"""Error parity (ADR 0011), the SAFE. prefix, and BigQuery's rules for arrays in written results."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import values


def raises(bq, sql: str, kind: ErrorKind = ErrorKind.RUNTIME) -> str:
    with pytest.raises(CrucibleError) as info:
        bq(sql)
    assert info.value.kind is kind, info.value.message
    return info.value.message


# --- division -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT 1 / 0 AS v", "division by zero: 1 / 0"),
        ("SELECT MOD(5, 0) AS v", "division by zero"),
        ("SELECT DIV(5, 0) AS v", "division by zero"),
    ],
)
def test_division_by_zero_raises(bq, sql, message):
    assert message in raises(bq, sql)


def test_division_from_data_and_safe_divide(bq):
    bq("CREATE TABLE t (a INT64, b INT64)")
    bq("INSERT INTO t VALUES (1, 0), (6, 3), (5, NULL)")
    assert "division by zero" in raises(bq, "SELECT a / b AS q FROM t")
    assert sorted(values(bq("SELECT SAFE_DIVIDE(a, b) AS q FROM t")), key=str) == [
        [2.0],
        [None],
        [None],
    ]


def test_ieee_divide_never_raises(bq):
    assert values(bq("SELECT IEEE_DIVIDE(1, 0) AS a, IEEE_DIVIDE(0, 0) AS b")) == [
        ["Infinity", "NaN"]
    ]


def test_volatile_divisor_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError):
        bq("SELECT 1 / RAND() AS v")


# --- casts --------------------------------------------------------------------------------------
def test_string_to_int64_accepts_bigquery_syntax(bq):
    result = bq(
        "SELECT CAST('0x1F' AS INT64) AS a, CAST('-0x1F' AS INT64) AS b, CAST('+5' AS INT64) AS c, "
        "CAST('-0x8000000000000000' AS INT64) AS d, CAST(CAST(NULL AS STRING) AS INT64) AS e"
    )
    assert values(result) == [[31, -31, 5, -(2**63), None]]


@pytest.mark.parametrize("text", ["1.5", "1e3", "", "x", "9223372036854775808"])
def test_string_to_int64_rejects_the_rest(bq, text):
    assert f"Bad int64 value: {text}" in raises(bq, f"SELECT CAST('{text}' AS INT64) AS v")
    assert values(bq(f"SELECT SAFE_CAST('{text}' AS INT64) AS v")) == [[None]]


def test_string_with_whitespace_to_int64_is_unsupported_until_recorded(bq):
    with pytest.raises(CrucibleUnsupportedError):
        bq("SELECT CAST(' 7 ' AS INT64) AS v")


def test_float_to_int64_rounds_half_away_from_zero(bq):
    result = bq(
        "SELECT CAST(-2.5 AS INT64) AS a, CAST(2.5 AS INT64) AS b, CAST(0.5 AS INT64) AS c, "
        "CAST(-0.5 AS INT64) AS d, SAFE_CAST(1.5 AS INT64) AS e"
    )
    assert values(result) == [[-3, 3, 1, -1, 2]]


def test_float_to_int64_from_data(bq):
    bq("CREATE TABLE t (x FLOAT64)")
    bq("INSERT INTO t VALUES (-2.5), (2.5), (0.5)")
    assert sorted(values(bq("SELECT CAST(x AS INT64) AS v FROM t"))) == [[-3], [1], [3]]


# --- OFFSET / ORDINAL ---------------------------------------------------------------------------
def test_offset_and_ordinal_in_range(bq):
    result = bq(
        "SELECT [1, 2][OFFSET(1)] AS a, [1, 2][ORDINAL(2)] AS b, [1, 2][SAFE_OFFSET(5)] AS c"
    )
    assert values(result) == [[2, 2, None]]


@pytest.mark.parametrize(
    ("index", "message"),
    [
        ("OFFSET(5)", "Array index 5 is out of bounds (overflow)"),
        ("OFFSET(-1)", "Array index -1 is out of bounds (underflow)"),
        ("ORDINAL(3)", "Array index 3 is out of bounds (overflow)"),
        ("ORDINAL(0)", "Array index 0 is out of bounds (underflow)"),
    ],
)
def test_offset_out_of_range_raises(bq, index, message):
    assert message in raises(bq, f"SELECT [1, 2][{index}] AS v")


def test_offset_of_null_array_or_index_is_null(bq):
    result = bq("SELECT CAST(NULL AS ARRAY<INT64>)[OFFSET(0)] AS a, [1][OFFSET(NULL)] AS b")
    assert values(result) == [[None, None]]


# --- overflow -----------------------------------------------------------------------------------
def test_int64_overflow_uses_bigquery_wording(bq):
    assert "integer overflow" in raises(bq, "SELECT 3000000000 * 4000000000 AS v")
    bq("CREATE TABLE t (a INT64)")
    bq("INSERT INTO t VALUES (9223372036854775807), (1)")
    assert "integer overflow" in raises(bq, "SELECT SUM(a) AS s FROM t")


# --- SAFE. --------------------------------------------------------------------------------------
def test_safe_prefix_suppresses_the_functions_own_error(bq):
    bq("CREATE TABLE t (j STRING, x FLOAT64)")
    bq("""INSERT INTO t VALUES ('{bad', -1), ('{"a": 1}', 1)""")
    result = bq("SELECT SAFE.PARSE_JSON(j) AS p, SAFE.LOG(x) AS l FROM t ORDER BY x")
    assert values(result) == [[None, None], [{"a": 1}, 0.0]]


def test_safe_prefix_keeps_errors_from_arguments(bq):
    assert "division by zero" in raises(bq, "SELECT SAFE.LOG(1 / 0) AS v")


def test_safe_prefix_on_aggregate_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError):
        bq("SELECT SAFE.SUM(x) AS v FROM UNNEST([1]) AS x")


# --- arrays in written results ------------------------------------------------------------------
def test_null_arrays_are_written_as_empty_arrays(bq):
    result = bq(
        "SELECT CAST(NULL AS ARRAY<INT64>) AS a, "
        "STRUCT(CAST(NULL AS ARRAY<INT64>) AS x, 1 AS y) AS s, "
        "ARRAY_LENGTH(CAST(NULL AS ARRAY<INT64>)) AS n"
    )
    assert values(result) == [[[], {"x": [], "y": 1}, None]]


def test_null_element_in_a_result_array_raises(bq):
    message = raises(bq, "SELECT [1, NULL] AS v")
    assert message == "Array cannot have a null element; error in writing field v"
    assert "null element" in raises(bq, "SELECT ARRAY_AGG(x) AS v FROM UNNEST([1, NULL]) AS x")


def test_stored_null_arrays_read_back_empty(bq):
    bq("CREATE TABLE t AS SELECT CAST(NULL AS ARRAY<INT64>) AS a")
    assert values(bq("SELECT a IS NULL AS n, a FROM t")) == [[False, []]]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT (1 / 0) / 2 AS v", "division by zero"),
        ("SELECT [[1][OFFSET(5)]][OFFSET(0)] AS v", "out of bounds"),
        ("SELECT CAST(CAST('1.5' AS INT64) AS FLOAT64) AS v", "Bad int64 value"),
    ],
)
def test_nested_checks_are_applied(bq, sql, message):
    assert message in raises(bq, sql)


def test_nested_safe(bq):
    assert values(bq("SELECT SAFE.LOG(SAFE.LOG(-1)) AS v")) == [[None]]
