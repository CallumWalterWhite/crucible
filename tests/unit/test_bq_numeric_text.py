"""NUMERIC as text (transforms/strings.py, common.numeric_text) and NUMERIC values in results."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlglot import exp

from crucible.engine import CrucibleError, CrucibleUnsupportedError
from crucible.server.bigquery.encoding import encode_value
from tests.unit.bigquery import column_types, values

LARGEST = "99999999999999999999999999999.999999999"


@pytest.mark.parametrize(
    ("numeric", "text"),
    [("4.10", "4.1"), ("4", "4"), ("400", "400"), ("-4.10", "-4.1"), ("0", "0"),
     ("0.000000001", "0.000000001"), (LARGEST, LARGEST), (f"-{LARGEST}", f"-{LARGEST}")],
)  # fmt: skip
def test_cast_and_safe_cast_of_numeric_to_string_drop_trailing_zeros(bq, numeric, text):
    sql = (
        f"SELECT CAST(NUMERIC '{numeric}' AS STRING) AS c, SAFE_CAST(NUMERIC '{numeric}' AS STRING)"
    )
    result = bq(sql)
    assert column_types(result)[0] == ("c", "STRING")
    assert values(result) == [[text, text]]


def test_a_numeric_of_scale_zero_keeps_its_zeros(bq):
    # DuckDB's ROUND gives DECIMAL(38,0), written "40": trimming zeros from it would give "4".
    sql = "SELECT CAST(ROUND(NUMERIC '40.4') AS STRING), TO_JSON_STRING(ROUND(NUMERIC '40.4'))"
    assert values(bq(sql)) == [["40", "40"]]


def test_cast_to_string_compares_with_bigquerys_text(bq):
    assert values(bq("SELECT CAST(NUMERIC '4.10' AS STRING) = '4.1'")) == [[True]]


def test_concat_and_the_operator_cast_numeric_like_cast(bq):
    sql = (
        "SELECT CONCAT('a', NUMERIC '4.10'), 'a' || NUMERIC '40', CAST(n AS STRING)"
        " FROM UNNEST([NUMERIC '1.50', NULL]) AS n"
    )
    assert values(bq(sql)) == [["a4.1", "a40", "1.5"], ["a4.1", "a40", None]]


@pytest.mark.parametrize(
    "sql",
    ["SELECT LENGTH(NUMERIC '1.5')", "SELECT UPPER(NUMERIC '1.5')",
     "SELECT NUMERIC '1.5' LIKE '1%'",
     "SELECT STRING_AGG(n) FROM UNNEST([NUMERIC '1.5']) AS n",
     "SELECT ARRAY_TO_STRING([NUMERIC '1.5'], ',')",
     "DECLARE x STRING DEFAULT NUMERIC '1.5'; SELECT x"],
)  # fmt: skip
def test_implicit_numeric_to_string_is_refused(bq, sql):
    with pytest.raises(CrucibleUnsupportedError, match="NUMERIC"):
        bq(sql)


def test_the_largest_numeric_literal_and_string_are_accepted_and_not_rounded(bq):
    sql = (
        f"SELECT NUMERIC '-{LARGEST}', CAST('{LARGEST}' AS NUMERIC),"
        " NUMERIC '12345678901234567890.123456789'"
    )
    assert values(bq(sql)) == [[f"-{LARGEST}", LARGEST, "12345678901234567890.123456789"]]
    with pytest.raises(CrucibleError, match="Invalid NUMERIC value: 1e29"):
        bq("SELECT CAST('1e29' AS NUMERIC)")


def test_a_numeric_result_cell_keeps_all_38_digits():
    numeric = exp.DataType.build("NUMERIC", dialect="bigquery")
    assert encode_value(Decimal(LARGEST), numeric) == LARGEST
    assert encode_value(Decimal("4.100000000"), numeric) == "4.1"
