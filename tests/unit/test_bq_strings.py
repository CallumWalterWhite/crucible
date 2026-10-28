"""FORMAT, UPPER/LOWER/INITCAP, FARM_FINGERPRINT (transforms/strings.py)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import column_types, values


# --- FORMAT: the specifiers that are translated -----------------------------------------------
def test_format_substitutes_arguments(bq):
    result = bq("SELECT FORMAT('%d-%s', 5, 'x') AS v")
    assert column_types(result) == [("v", "STRING")]
    assert values(result) == [["5-x"]]


def test_format_i_is_decimal(bq):
    assert values(bq("SELECT FORMAT('%i', 42) AS v")) == [["42"]]


def test_format_escaped_percent_is_one_percent(bq):
    assert values(bq("SELECT FORMAT('100%%') AS v")) == [["100%"]]


def test_format_float_defaults_to_six_decimals(bq):
    assert values(bq("SELECT FORMAT('%f', 1.5) AS v")) == [["1.500000"]]


def test_format_float_two_places_rounds_like_printf(bq):
    # 1.005 is 1.00499... in binary, so two places print 1.00 (recorded BigQuery answer).
    assert values(bq("SELECT FORMAT('%.2f', 1.005) AS v")) == [["1.00"]]


def test_format_numeric_with_exact_decimals(bq):
    assert values(bq("SELECT FORMAT('%.2f', NUMERIC '1.5') AS v")) == [["1.50"]]


def test_format_numeric_that_needs_rounding_is_unsupported_at_run_time(bq):
    # BigQuery's rounding of NUMERIC is not documented here, so the value is refused.
    with pytest.raises(CrucibleUnsupportedError, match="needs rounding"):
        bq("SELECT FORMAT('%.2f', NUMERIC '1.005') AS v")


def test_format_numeric_with_too_many_digits_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match="too many digits"):
        bq("SELECT FORMAT('%.6f', NUMERIC '12345678901.5') AS v")


def test_format_float_non_finite_values_print_in_lowercase(bq):
    # The documentation: %f prints inf, -inf and nan in lowercase for non-finite values.
    assert values(bq("SELECT FORMAT('%f', CAST('inf' AS FLOAT64)) AS v")) == [["inf"]]
    assert values(bq("SELECT FORMAT('%f', CAST('nan' AS FLOAT64)) AS v")) == [["nan"]]


# --- FORMAT: NULL -------------------------------------------------------------------------------
def test_format_null_argument_gives_null(bq):
    assert values(bq("SELECT FORMAT('%d', NULL) AS v")) == [[None]]
    assert values(bq("SELECT FORMAT('%s', CAST(NULL AS STRING)) AS v")) == [[None]]


def test_format_null_argument_from_a_table_gives_null_for_that_row(bq):
    bq("CREATE TABLE t (n INT64)")
    bq("INSERT INTO t VALUES (7), (NULL)")
    result = bq("SELECT FORMAT('n=%d', n) AS v FROM t")
    assert sorted(values(result), key=str) == sorted([["n=7"], [None]], key=str)


def test_format_null_format_string_gives_null(bq):
    assert values(bq("SELECT FORMAT(NULL, 1) AS v")) == [[None]]


# --- FORMAT: refusals ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "construct"),
    [
        ("SELECT FORMAT('%5d', 1) AS v", "FORMAT specifier '%5d'"),
        ("SELECT FORMAT('%F', 1.5) AS v", "FORMAT specifier '%F'"),
        ("SELECT FORMAT('%', 1) AS v", "FORMAT specifier '%'"),
        ("SELECT FORMAT('%d', 1.5) AS v", "FORMAT %d with a FLOAT64 argument"),
        ("SELECT FORMAT('%s', 5) AS v", "FORMAT %s with a INT64 argument"),
    ],
)
def test_unsupported_specifiers_and_argument_types(bq, sql, construct):
    with pytest.raises(CrucibleUnsupportedError, match=construct):
        bq(sql)


def test_argument_count_mismatch_is_an_invalid_query(bq):
    with pytest.raises(CrucibleError) as info:
        bq("SELECT FORMAT('%d') AS v")
    assert info.value.kind is ErrorKind.INVALID_QUERY


def test_non_literal_format_string_is_unsupported(bq):
    bq("CREATE TABLE t (f STRING)")
    bq("INSERT INTO t VALUES ('%d')")
    with pytest.raises(CrucibleUnsupportedError, match="not a literal"):
        bq("SELECT FORMAT(f, 1) AS v FROM t")


# --- UPPER / LOWER / INITCAP -------------------------------------------------------------------
def test_ascii_is_unchanged_by_the_guard(bq):
    result = bq("SELECT UPPER('hello') AS u, LOWER('HeLLo') AS l, INITCAP('hello wORLD') AS i")
    assert column_types(result) == [("u", "STRING"), ("l", "STRING"), ("i", "STRING")]
    assert values(result) == [["HELLO", "hello", "Hello World"]]


def test_non_ascii_letters_without_multi_character_mapping(bq):
    assert values(bq("SELECT LOWER('ÄB') AS v")) == [["äb"]]


def test_null_in_gives_null_out(bq):
    result = bq(
        "SELECT UPPER(CAST(NULL AS STRING)) AS u, LOWER(CAST(NULL AS STRING)) AS l, "
        "INITCAP(CAST(NULL AS STRING)) AS i"
    )
    assert values(result) == [[None, None, None]]


@pytest.mark.parametrize(
    ("sql", "construct"),
    [
        ("SELECT UPPER('ß') AS v", "UPPER of a character with a multi-character case mapping"),
        ("SELECT LOWER('ΣΑΣ') AS v", "LOWER of a capital sigma"),
        ("SELECT INITCAP('ß') AS v", "INITCAP of a character with a multi-character case mapping"),
    ],
)
def test_multi_character_case_mappings_are_unsupported(bq, sql, construct):
    with pytest.raises(CrucibleUnsupportedError, match=construct):
        bq(sql)


def test_a_nested_call_is_guarded_too(bq):
    # The inner UPPER('ß') must raise even though it sits inside another UPPER.
    with pytest.raises(CrucibleUnsupportedError, match="UPPER of a character"):
        bq("SELECT UPPER(UPPER('ß')) AS v")


def test_one_special_row_refuses_the_whole_column(bq):
    bq("CREATE TABLE t (s STRING)")
    bq("INSERT INTO t VALUES ('plain'), ('ß')")
    with pytest.raises(CrucibleUnsupportedError, match="UPPER of a character"):
        bq("SELECT UPPER(s) AS v FROM t")


# --- FARM_FINGERPRINT ----------------------------------------------------------------------------
def test_farm_fingerprint_matches_bigquery_for_recorded_input(bq):
    # Recorded: hash/farm_fingerprint. The empty input is FarmHash's k2 constant, signed.
    assert values(bq("SELECT FARM_FINGERPRINT('a'), FARM_FINGERPRINT('')")) == [
        [-5528939962900187677, -7286425919675154353]
    ]


def test_farm_fingerprint_of_a_string_is_that_of_its_utf8_bytes(bq):
    [[s, b, n]] = values(
        bq(
            "SELECT FARM_FINGERPRINT('é'), FARM_FINGERPRINT(b'\\xc3\\xa9'), "
            "FARM_FINGERPRINT(CAST(NULL AS STRING))"
        )
    )
    assert s == b
    assert n is None


def test_farm_fingerprint_of_another_type_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match="FARM_FINGERPRINT of a INT64"):
        bq("SELECT FARM_FINGERPRINT(1)")


def test_lower_uses_full_case_mapping_for_dotted_capital_i(bq):
    # Recorded: LOWER('İ') is 'i' followed by U+0307 COMBINING DOT ABOVE.
    assert values(bq("SELECT LOWER('İ') AS v")) == [["i\u0307"]]


# --- LIKE: a backslash escapes `%`, `_` and itself ---------------------------------------------
def test_like_backslash_escapes_wildcards(bq):
    sql = (
        r"SELECT 'a%' LIKE 'a\\%', 'ab' LIKE 'a\\%', 'a_' LIKE 'a\\_', 'ab' LIKE 'a\\_', "
        r"'a\\b' LIKE 'a\\\\b'"
    )
    assert values(bq(sql)) == [[True, False, True, False, True]]


def test_like_pattern_ending_in_a_lone_backslash_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match="unescaped backslash"):
        bq(r"SELECT 'a' LIKE 'a\\'")
