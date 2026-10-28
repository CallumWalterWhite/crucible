"""STRING and BYTES conversions (transforms/binary.py)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleUnsupportedError
from tests.unit.bigquery import column_types, values


def test_cast_string_to_bytes_is_utf8_not_escapes(bq):
    result = bq(r"SELECT CAST('\\x41' AS BYTES) AS a, CAST('é' AS BYTES) AS b")
    assert column_types(result) == [("a", "BYTES"), ("b", "BYTES")]
    assert values(result) == [["XHg0MQ==", "w6k="]]  # the 4 bytes \x41; C3 A9


def test_cast_bytes_to_string_decodes_utf8(bq):
    result = bq(r"SELECT CAST(b'\xc3\xa9' AS STRING) AS a, CAST(b'\\x41' AS STRING) AS b")
    assert values(result) == [["é", "\\x41"]]


def test_cast_of_null_stays_null(bq):
    sql = "SELECT CAST(CAST(NULL AS STRING) AS BYTES), CAST(CAST(NULL AS BYTES) AS STRING)"
    assert values(bq(sql)) == [[None, None]]


def test_invalid_utf8_is_unsupported_and_null_under_safe_cast(bq):
    with pytest.raises(CrucibleUnsupportedError, match="not UTF-8"):
        bq(r"SELECT CAST(b'\xff' AS STRING) AS v")
    assert values(bq(r"SELECT SAFE_CAST(b'\xff' AS STRING) AS v")) == [[None]]


def test_cast_over_a_column_round_trips(bq):
    sql = "SELECT CAST(CAST(s AS BYTES) AS STRING) AS v FROM UNNEST(['é日本', '\\\\xff']) AS s"
    assert sorted(row[0] for row in values(bq(sql))) == ["\\xff", "é日本"]


def test_bytes_concat_operator_is_bytes(bq):
    result = bq(r"SELECT b'a' || b'\xff' AS v")
    assert column_types(result) == [("v", "BYTES")]
    assert values(result) == [["Yf8="]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CONCAT('a', b'b')",
        "SELECT 'a' || b'b'",
        "SELECT 'A' = b'A'",
        "SELECT 'a' IN (b'a')",
        "SELECT COALESCE('x', b'y')",
        "SELECT IF(TRUE, 'x', b'y')",
        "SELECT CASE WHEN TRUE THEN 'x' ELSE b'y' END",
        "SELECT [b'a', 'b']",
        "SELECT GREATEST('a', b'b')",
    ],
)
def test_string_and_bytes_operands_are_refused(bq, sql):
    with pytest.raises(CrucibleUnsupportedError, match="STRING and BYTES operands"):
        bq(sql)


@pytest.mark.parametrize(
    "call",
    ["REVERSE(b'ab')", "LTRIM(b'xa', b'x')", "TRIM(b'a')", "REPLACE(b'ab', b'a', b'c')",
     "STARTS_WITH(b'ab', b'a')", "UPPER(b'a')", "STRING(b'a')"],
)  # fmt: skip
def test_functions_on_the_text_form_of_bytes_are_refused(bq, call):
    with pytest.raises(CrucibleUnsupportedError, match="of BYTES"):
        bq(f"SELECT {call}")


def test_struct_fields_of_both_types_are_allowed(bq):
    assert values(bq("SELECT STRUCT('a' AS s, b'b' AS y) AS v")) == [[{"s": "a", "y": "Yg=="}]]


def test_variable_of_the_other_type_is_refused(bq):
    with pytest.raises(CrucibleUnsupportedError, match="STRING value to a BYTES variable"):
        bq("DECLARE x BYTES DEFAULT 'a'; SELECT x")


def test_to_json_string_of_bare_bytes_is_a_json_string(bq):
    assert values(bq(r"SELECT TO_JSON_STRING(b'\xff') AS v")) == [['"/w=="']]
