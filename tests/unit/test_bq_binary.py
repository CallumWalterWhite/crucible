"""STRING and BYTES conversions (transforms/binary.py)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
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


def test_invalid_utf8_is_a_runtime_error_and_null_under_safe_cast(bq):
    with pytest.raises(CrucibleError, match=r"^Invalid cast of bytes to UTF8 string$") as info:
        bq(r"SELECT CAST(s AS STRING) AS v FROM UNNEST([b'a', b'\xff']) AS s")
    assert info.value.kind is ErrorKind.RUNTIME
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
        "SELECT CONCAT('a', b'b', 1)",
        "SELECT 'a' || b'b'",
        "SELECT b'A' = 'A'",
        "SELECT 'A' < b'A'",
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
    ("call", "name"),
    [("LTRIM(b'xa', b'x')", "LTRIM"), ("TRIM(b'a')", "TRIM"),
     ("REPLACE(b'ab', b'a', b'c')", "REPLACE"), ("UPPER(b'a')", "UPPER"),
     ("STRING(b'a')", "STRING"),
     ("b'ab' LIKE b'a%'", "LIKE"), ("b'ab' NOT LIKE b'a%'", "LIKE"), ("SUBSTR(b'ab', 2)", "SUBSTR"),
     ("SUBSTRING(b'ab', 1, 1)", "SUBSTRING"), ("SPLIT(b'a,b', b',')", "SPLIT"),
     ("REGEXP_CONTAINS(b'ab', b'b')", "REGEXP_CONTAINS"),
     ("REGEXP_REPLACE(b'ab', b'b', b'c')", "REGEXP_REPLACE"), ("CHAR_LENGTH(b'a')", "CHAR_LENGTH"),
     ("ARRAY_TO_STRING([b'a'], b',')", "ARRAY_TO_STRING"),
     ("STRPOS(b'\\x12\\x34', b'\\x23')", "STRPOS"), ("INSTR(b'ab', b'b')", "INSTR"),
     ("LPAD(b'a', 4, b'xy')", "LPAD"), ("RPAD(b'abc', 2)", "RPAD")],
)  # fmt: skip
def test_bytes_functions_without_an_exact_form_are_refused(bq, call, name):
    with pytest.raises(
        CrucibleUnsupportedError, match=f"^Not supported by Crucible: {name} of BYTES$"
    ):
        bq(f"SELECT {call}")


def test_reverse_starts_with_and_ends_with_work_on_whole_bytes(bq):
    sql = (
        r"SELECT REVERSE(b'\x01\xff\\'), REVERSE(b''), STARTS_WITH(b'\x12\x34', b'\x12'),"
        r" ENDS_WITH(b'\x12\x34', b'\x34'), ENDS_WITH(b'\x12\x34', b'\x23')"
    )
    assert values(bq(sql)) == [["XP8B", "", True, True, False]]


def test_bit_count_of_bytes_and_of_int64(bq):
    sql = "SELECT BIT_COUNT(b'\\xff\\x01'), BIT_COUNT(b''), BIT_COUNT(x) FROM UNNEST([-1, 5]) AS x"
    assert values(bq(sql)) == [[9, 0, 64], [9, 0, 2]]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("STRING_AGG(x, b'\\xff' ORDER BY x)", "Af8B/xD/YQ=="),
        ("STRING_AGG(DISTINCT x, b'-' ORDER BY x DESC)", "YS0QLQE="),
    ],
)
def test_string_agg_of_bytes_with_a_bytes_delimiter(bq, call, expected):
    sql = f"SELECT {call} AS v FROM UNNEST([b'\\x10', b'\\x01', b'a', b'\\x01']) AS x"
    assert values(bq(sql)) == [[expected]]


def test_string_agg_of_bytes_over_a_window(bq):
    sql = "SELECT STRING_AGG(x, b',') OVER (ORDER BY x) FROM UNNEST([b'a', b'b']) AS x"
    assert values(bq(sql)) == [["YQ=="], ["YSxi"]]


def test_concat_of_string_and_bytes_is_bigquerys_signature_error(bq):
    with pytest.raises(CrucibleError) as info:
        bq("SELECT CONCAT(b'a', 'b', b'c')")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert str(info.value) == (
        "No matching signature for function CONCAT\n  Argument types: BYTES, STRING, BYTES\n"
        "  Signature: CONCAT(STRING, [STRING, ...])\n"
        "    Argument 1: Unable to coerce type BYTES to expected type STRING\n"
        "  Signature: CONCAT(BYTES, [BYTES, ...])\n"
        "    Argument 2: Unable to coerce type STRING to expected type BYTES at [1:8]"
    )


def test_string_equals_bytes_is_bigquerys_signature_error(bq):
    with pytest.raises(CrucibleError, match=r"^No matching signature for operator = "):
        bq("SELECT 'A' = b'A'")


def test_struct_fields_of_both_types_are_allowed(bq):
    assert values(bq("SELECT STRUCT('a' AS s, b'b' AS y) AS v")) == [[{"s": "a", "y": "Yg=="}]]


def test_a_string_literal_for_a_bytes_variable_is_bigquerys_coercion_error(bq):
    with pytest.raises(
        CrucibleError,
        match=r"^Query error: Cannot coerce expression 'a' to type BYTES at \[1:25\]$",
    ):
        bq("DECLARE x BYTES DEFAULT 'a'; SELECT x")


@pytest.mark.parametrize(
    "script",
    ["DECLARE x STRING DEFAULT b'a'; SELECT x", "DECLARE x BYTES DEFAULT CONCAT('a'); SELECT x"],
)
def test_other_values_of_the_other_type_for_a_variable_are_refused(bq, script):
    with pytest.raises(CrucibleUnsupportedError, match="variable"):
        bq(script)


def test_to_json_string_of_bare_bytes_is_a_json_string(bq):
    assert values(bq(r"SELECT TO_JSON_STRING(b'\xff') AS v")) == [['"/w=="']]
