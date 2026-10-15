"""TIMESTAMP_DIFF, EXTRACT(DAYOFWEEK|WEEK) and FORMAT_* `%e` (transforms/dates.py)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import column_types, values


# --- TIMESTAMP_DIFF: whole parts, truncated toward zero --------------------------------------
@pytest.mark.parametrize(
    ("later", "earlier", "part", "expected"),
    [
        ("'2024-01-02 00:00:00+00'", "'2024-01-01 12:00:00+00'", "DAY", 0),  # 12 hours is 0 days
        ("'2024-01-03 00:00:00+00'", "'2024-01-01 00:00:00+00'", "DAY", 2),
        ("'2024-01-02 12:00:00+00'", "'2024-01-01 00:00:00+00'", "DAY", 1),  # 1.5 days is 1
        # Negative: truncated toward zero, so -1.5 days is -1, not -2.
        ("'2024-01-01 00:00:00+00'", "'2024-01-02 12:00:00+00'", "DAY", -1),
        ("'2024-01-01 00:00:00+00'", "'2024-01-01 00:59:59+00'", "MINUTE", -59),
        ("'2024-01-01 00:59:00+00'", "'2024-01-01 00:00:00+00'", "HOUR", 0),
        ("'2024-01-01 01:00:00+00'", "'2024-01-01 00:00:00+00'", "HOUR", 1),
        ("'2024-01-01 00:00:01+00'", "'2024-01-01 00:00:00+00'", "MICROSECOND", 1000000),
        ("'2024-01-01 00:00:01+00'", "'2024-01-01 00:00:00+00'", "MILLISECOND", 1000),
        ("'2024-01-01 00:00:02+00'", "'2024-01-01 00:00:00+00'", "SECOND", 2),
    ],
)
def test_counts_whole_parts(bq, later, earlier, part, expected):
    result = bq(f"SELECT TIMESTAMP_DIFF(TIMESTAMP {later}, TIMESTAMP {earlier}, {part}) AS v")
    assert column_types(result) == [("v", "INT64")]
    assert values(result) == [[expected]]


def test_null_input_is_null(bq):
    result = bq("SELECT TIMESTAMP_DIFF(CAST(NULL AS TIMESTAMP), TIMESTAMP '2024-01-01', DAY) AS v")
    assert values(result) == [[None]]


def test_an_unsupported_date_part_is_an_invalid_query(bq):
    with pytest.raises(CrucibleError) as info:
        bq("SELECT TIMESTAMP_DIFF(TIMESTAMP '2024-01-08', TIMESTAMP '2024-01-01', WEEK) AS v")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert "TIMESTAMP_DIFF does not support the WEEK date part" in info.value.message


# --- EXTRACT(DAYOFWEEK): Sunday is 1 ----------------------------------------------------------
@pytest.mark.parametrize(
    ("day", "expected"),
    [("2024-03-03", 1), ("2024-03-04", 2), ("2024-03-09", 7), ("2024-03-02", 7)],
)
def test_dayofweek_sunday_is_one(bq, day, expected):
    result = bq(f"SELECT EXTRACT(DAYOFWEEK FROM DATE '{day}') AS v")
    assert column_types(result) == [("v", "INT64")]
    assert values(result) == [[expected]]


def test_dayofweek_null_is_null(bq):
    assert values(bq("SELECT EXTRACT(DAYOFWEEK FROM CAST(NULL AS DATE)) AS v")) == [[None]]


# --- EXTRACT(WEEK): not translated -----------------------------------------------------------
@pytest.mark.parametrize("part", ["WEEK", "WEEK(MONDAY)"])
def test_week_extraction_is_unsupported(bq, part):
    with pytest.raises(CrucibleUnsupportedError, match="EXTRACT\\(WEEK\\)"):
        bq(f"SELECT EXTRACT({part} FROM DATE '2024-01-07') AS v")


# --- FORMAT_DATE / FORMAT_DATETIME / FORMAT_TIMESTAMP: `%e` pads the day to two characters ----
def test_format_date_pads_a_one_digit_day(bq):
    result = bq("SELECT FORMAT_DATE('%b %e %Y', DATE '2024-03-05') AS v")
    assert column_types(result) == [("v", "STRING")]
    assert values(result) == [["Mar  5 2024"]]


def test_format_date_two_digit_day_is_unchanged(bq):
    assert values(bq("SELECT FORMAT_DATE('%e', DATE '2024-03-15') AS v")) == [["15"]]


def test_format_date_without_e_is_unchanged(bq):
    assert values(bq("SELECT FORMAT_DATE('%Y-%m', DATE '2024-03-05') AS v")) == [["2024-03"]]


def test_format_date_null_is_null(bq):
    assert values(bq("SELECT FORMAT_DATE('%e', CAST(NULL AS DATE)) AS v")) == [[None]]


def test_escaped_percent_before_day_is_a_literal_percent(bq):
    # '%%' is one literal percent, and '%e' after it is still the padded day.
    assert values(bq("SELECT FORMAT_DATE('%%%e', DATE '2024-03-05') AS v")) == [["% 5"]]


def test_escaped_percent_e_is_unsupported(bq):
    # SQLGlot turns BigQuery's '%%e' into DuckDB's '%%-d', which prints the wrong text. Refused.
    with pytest.raises(CrucibleUnsupportedError, match="with %%e"):
        bq("SELECT FORMAT_DATE('%%e', DATE '2024-03-05') AS v")


def test_format_timestamp_and_datetime_pad_the_day(bq):
    ts = bq("SELECT FORMAT_TIMESTAMP('%e %H', TIMESTAMP '2024-03-05 10:00:00+00') AS v")
    assert values(ts) == [[" 5 10"]]
    dt = bq("SELECT FORMAT_DATETIME('%e', DATETIME '2024-03-05 10:00:00') AS v")
    assert values(dt) == [[" 5"]]
