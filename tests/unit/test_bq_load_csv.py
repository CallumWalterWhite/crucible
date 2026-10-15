"""CSV load parsing against the recorded BigQuery load fixtures (server/bigquery/load.py).

The CSV strings below are copied from `git show 941942f:src/crucible/corpus/record_api.py`
(LOAD_CASES and LOAD_SCHEMA). Each expected outcome is read from the fixture that BigQuery answered
with, not written from the parser's output.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from crucible.engine import CrucibleUnsupportedError
from crucible.server.bigquery.load import LoadError, parse_csv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bigquery_api"

# 941942f LOAD_SCHEMA
SCHEMA: list[dict[str, str]] = [
    {"name": "id", "type": "INTEGER"},
    {"name": "b", "type": "BOOLEAN"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "d", "type": "DATE"},
    {"name": "n", "type": "NUMERIC"},
    {"name": "f", "type": "FLOAT"},
    {"name": "s", "type": "STRING"},
]
# 941942f _HEADER
HEADER = "id,b,ts,d,n,f,s\n"
# 941942f LOAD_CASES, verbatim
BASELINE = (
    HEADER
    + '1,true,2024-01-02 03:04:05,2024-01-02,1.5,1.5,"quoted, with comma"\n'
    + "2,false,2024-01-02 03:04:05.123456,2024-01-02,-0.000000001,-1.5,plain\n"
)
EMPTY_FIELDS = HEADER + '1,,,,,,\n2,true,2024-01-02,2024-01-02,1,1,""\n'
BOOL_T = HEADER + "1,T,2024-01-02,2024-01-02,1,1,x\n"
BOOL_1 = HEADER + "1,1,2024-01-02,2024-01-02,1,1,x\n"
BOOL_YES = HEADER + "1,yes,2024-01-02,2024-01-02,1,1,x\n"
BOOL_MIXED_CASE = HEADER + "1,False,2024-01-02,2024-01-02,1,1,x\n"
TS_T_Z = HEADER + "1,true,2024-01-02T03:04:05Z,2024-01-02,1,1,x\n"
TS_UTC_SUFFIX = HEADER + "1,true,2024-01-02 03:04:05.123456 UTC,2024-01-02,1,1,x\n"
TS_OFFSET = HEADER + "1,true,2024-01-02 03:04:05+01:00,2024-01-02,1,1,x\n"
TS_EPOCH = HEADER + "1,true,1704164645,2024-01-02,1,1,x\n"
DATE_ONE_DIGIT = HEADER + "1,true,2024-01-02,2024-1-2,1,1,x\n"
NUMERIC_EXPONENT = HEADER + "1,true,2024-01-02,2024-01-02,1e3,1,x\n"
FLOAT_INF_NAN = (
    HEADER + "1,true,2024-01-02,2024-01-02,1,-inf,x\n2,true,2024-01-02,2024-01-02,1,NaN,y\n"
)
BAD_VALUE = HEADER + "1,maybe,2024-01-02,2024-01-02,1,1,x\n"


def fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return body


def recorded_errors(jobs_get: str) -> list[str]:
    """The messages BigQuery returned for a failed load job, summary first."""
    status = fixture(jobs_get)["body"]["status"]
    return [error["message"] for error in status["errors"]]


def wire_value(value: str | None, kind: str) -> Any:
    """A query-results cell as a Python value (the REST wire format is all strings)."""
    if value is None:
        return None
    if kind == "INTEGER":
        return int(value)
    if kind == "BOOLEAN":
        return value == "true"
    if kind == "TIMESTAMP":  # epoch seconds, as a decimal string in exponent form
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=int(Decimal(value) * 10**6)
        )
    if kind == "DATE":
        return date.fromisoformat(value)
    if kind == "NUMERIC":
        return Decimal(value)
    if kind == "FLOAT":
        return float(value)
    return value


def recorded_rows(query_results: str) -> list[tuple[Any, ...]]:
    body = fixture(query_results)["body"]
    kinds = [field["type"] for field in body["schema"]["fields"]]
    return [
        tuple(wire_value(cell["v"], kind) for cell, kind in zip(row["f"], kinds, strict=True))
        for row in body.get("rows", [])
    ]


# --- recorded outcomes: rows ---------------------------------------------------------------------
def test_baseline_loads_the_recorded_rows():
    # 941942f LOAD_CASES["baseline"]; fixtures 25 (query results of the loaded table).
    assert parse_csv(BASELINE.encode(), SCHEMA, 1) == recorded_rows(
        "25_load_baseline_query_results.json"
    )


@pytest.mark.parametrize(
    ("csv_text", "query_results"),
    [
        (TS_T_Z, "50_load_ts_t_z_query_results.json"),
        (TS_UTC_SUFFIX, "55_load_ts_utc_suffix_query_results.json"),
        (TS_OFFSET, "60_load_ts_offset_query_results.json"),
        (TS_EPOCH, "65_load_ts_epoch_query_results.json"),
    ],
)
def test_timestamp_spellings_load_the_recorded_value(csv_text, query_results):
    assert parse_csv(csv_text.encode(), SCHEMA, 1) == recorded_rows(query_results)


def test_offset_is_converted_to_utc():
    # 03:04:05+01:00 is 02:04:05 UTC (fixture 60), not 03:04:05 UTC.
    [row] = parse_csv(TS_OFFSET.encode(), SCHEMA, 1)
    assert row[2] == datetime(2024, 1, 2, 2, 4, 5, tzinfo=UTC)


def test_a_negative_offset_is_added():
    [row] = parse_csv(
        (HEADER + "1,true,2024-01-02 03:04:05-05:00,2024-01-02,1,1,x\n").encode(), SCHEMA, 1
    )
    assert row[2] == datetime(2024, 1, 2, 8, 4, 5, tzinfo=UTC)


# --- recorded outcomes: LoadError messages -------------------------------------------------------
@pytest.mark.parametrize(
    ("csv_text", "jobs_get"),
    [
        (EMPTY_FIELDS, "28_load_empty_fields_jobs_get.json"),
        (DATE_ONE_DIGIT, "68_load_date_one_digit_jobs_get.json"),
        (NUMERIC_EXPONENT, "72_load_numeric_exponent_jobs_get.json"),
        (FLOAT_INF_NAN, "76_load_float_inf_nan_jobs_get.json"),
        (BAD_VALUE, "80_load_bad_value_jobs_get.json"),
    ],
)
def test_rejected_loads_give_the_recorded_messages(csv_text, jobs_get):
    with pytest.raises(LoadError) as info:
        parse_csv(csv_text.encode(), SCHEMA, 1)
    assert info.value.messages == recorded_errors(jobs_get)


def test_the_summary_counts_every_data_row_and_the_rejected_ones():
    # 28: two data rows, one rejected (line 3, byte offset 24). 76: two rejected rows.
    with pytest.raises(LoadError) as info:
        parse_csv(EMPTY_FIELDS.encode(), SCHEMA, 1)
    assert info.value.messages[0].endswith("Rows: 2; errors: 1; max bad: 0; error percent: 0")
    assert "line_number: 3 byte_offset_to_start_of_line: 24" in info.value.messages[1]


# --- recorded outcomes: unsupported (accepted by BigQuery, value not recorded) -------------------
@pytest.mark.parametrize(
    ("csv_text", "value", "jobs_get"),
    [
        (BOOL_T, "T", "32_load_bool_t_jobs_get.json"),
        (BOOL_1, "1", "36_load_bool_1_jobs_get.json"),
        (BOOL_YES, "yes", "40_load_bool_yes_jobs_get.json"),
        (BOOL_MIXED_CASE, "False", "44_load_bool_mixed_case_jobs_get.json"),
    ],
)
def test_accepted_boolean_spellings_are_unsupported(csv_text, value, jobs_get):
    # BigQuery accepted the spelling and only rejected the date-only timestamp after it (fixtures
    # 32-44). The loaded boolean is not recorded, so the load is unsupported, not rejected.
    assert any('column_name: "ts"' in message for message in recorded_errors(jobs_get))
    with pytest.raises(CrucibleUnsupportedError, match=f"CSV value '{value}' for BOOL column b"):
        parse_csv(csv_text.encode(), SCHEMA, 1)


# --- rules: tokenizing ---------------------------------------------------------------------------
def test_a_byte_order_mark_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="byte order mark"):
        parse_csv(b"\xef\xbb\xbf" + BASELINE.encode(), SCHEMA, 1)


def test_crlf_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="carriage return"):
        parse_csv(BASELINE.replace("\n", "\r\n").encode(), SCHEMA, 1)


def test_a_quote_inside_a_quoted_field_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="escaped quote"):
        parse_csv((HEADER + '1,true,2024-01-02,2024-01-02,1,1,"a""b"\n').encode(), SCHEMA, 1)


def test_a_quote_inside_an_unquoted_field_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="unquoted field"):
        parse_csv((HEADER + '1,true,2024-01-02,2024-01-02,1,1,a"b\n').encode(), SCHEMA, 1)


def test_text_after_a_closing_quote_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="after a closing quote"):
        parse_csv((HEADER + '1,true,2024-01-02,2024-01-02,1,1,"a"b\n').encode(), SCHEMA, 1)


def test_a_blank_line_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="blank line"):
        parse_csv((HEADER + "\n").encode(), SCHEMA, 1)


def test_a_wrong_column_count_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="fields for 7 columns"):
        parse_csv((HEADER + "1,true,2024-01-02\n").encode(), SCHEMA, 1)
    with pytest.raises(CrucibleUnsupportedError, match="fields for 7 columns"):
        parse_csv((HEADER + "1,true,2024-01-02,2024-01-02,1,1,x,extra\n").encode(), SCHEMA, 1)


def test_a_file_without_a_final_newline_loads():
    assert parse_csv((HEADER + "1,true,2024-01-02 03:04:05,2024-01-02,1,1,x").encode(), SCHEMA, 1)


def test_skipping_leading_rows_is_by_count():
    rows = parse_csv(("junk\n" + BASELINE).encode(), SCHEMA, 2)
    assert len(rows) == 2


def test_the_header_as_data_is_unsupported():
    # skip_leading_rows=0 reads the header as a row; "id" is not an INTEGER.
    with pytest.raises(CrucibleUnsupportedError, match="CSV value 'id' for INT64 column id"):
        parse_csv(BASELINE.encode(), SCHEMA, 0)


def test_a_negative_skip_is_an_error():
    with pytest.raises(ValueError, match="skip_leading_rows"):
        parse_csv(BASELINE.encode(), SCHEMA, -1)


# --- rules: values -------------------------------------------------------------------------------
def test_an_unquoted_empty_field_is_null_for_every_type():
    schema = [
        {"name": "i", "type": "INTEGER"},
        {"name": "b", "type": "BOOLEAN"},
        {"name": "ts", "type": "TIMESTAMP"},
        {"name": "d", "type": "DATE"},
        {"name": "n", "type": "NUMERIC"},
        {"name": "f", "type": "FLOAT"},
        {"name": "s", "type": "STRING"},
    ]
    assert parse_csv(b"i,b,ts,d,n,f,s\n,,,,,,\n", schema, 1) == [(None,) * 7]


def test_a_quoted_empty_field_is_unsupported():
    # A row with no rejected column reaches the quoted empty value, which is unsupported.
    with pytest.raises(CrucibleUnsupportedError, match="quoted empty value for STRING column s"):
        parse_csv((HEADER + '1,true,2024-01-02 03:04:05,2024-01-02,1,1,""\n').encode(), SCHEMA, 1)


def test_a_quoted_value_for_a_non_string_column_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="quoted value '1' for INT64 column id"):
        parse_csv((HEADER + '"1",true,2024-01-02 03:04:05,2024-01-02,1,1,x\n').encode(), SCHEMA, 1)


def test_an_integer_out_of_range_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="out of INT64 range"):
        parse_csv(
            (HEADER + "9223372036854775808,true,2024-01-02 03:04:05,2024-01-02,1,1,x\n").encode(),
            SCHEMA,
            1,
        )


def test_the_largest_integer_loads():
    [row] = parse_csv(
        (HEADER + "9223372036854775807,true,2024-01-02 03:04:05,2024-01-02,1,1,x\n").encode(),
        SCHEMA,
        1,
    )
    assert row[0] == 2**63 - 1


@pytest.mark.parametrize("numeric", ["1.0000000001", "1e3", "+1", "1."])
def test_numeric_beyond_the_confirmed_spellings_is_unsupported(numeric):
    with pytest.raises(CrucibleUnsupportedError, match="CSV value"):
        parse_csv(
            (HEADER + f"1,true,2024-01-02 03:04:05,2024-01-02,{numeric},1,x\n").encode(), SCHEMA, 1
        )


def test_numeric_with_nine_fraction_digits_loads_exactly():
    [row] = parse_csv(
        (HEADER + "1,true,2024-01-02 03:04:05,2024-01-02,-0.000000001,1,x\n").encode(), SCHEMA, 1
    )
    assert row[4] == Decimal("-0.000000001")


@pytest.mark.parametrize("value", ["inf", "NaN", "1e3", "+1.5"])
def test_float_spellings_beyond_the_confirmed_are_unsupported(value):
    with pytest.raises(CrucibleUnsupportedError, match="CSV value"):
        parse_csv(
            (HEADER + f"1,true,2024-01-02 03:04:05,2024-01-02,1,{value},x\n").encode(), SCHEMA, 1
        )


def test_an_invalid_calendar_date_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="CSV value '2024-02-30' for DATE column d"):
        parse_csv((HEADER + "1,true,2024-01-02 03:04:05,2024-02-30,1,1,x\n").encode(), SCHEMA, 1)


def test_a_timestamp_with_six_fraction_digits_only():
    with pytest.raises(CrucibleUnsupportedError, match="for TIMESTAMP column ts"):
        parse_csv(
            (HEADER + "1,true,2024-01-02 03:04:05.123,2024-01-02,1,1,x\n").encode(), SCHEMA, 1
        )


def test_the_t_separator_needs_the_z_suffix():
    with pytest.raises(CrucibleUnsupportedError, match="for TIMESTAMP column ts"):
        parse_csv((HEADER + "1,true,2024-01-02T03:04:05,2024-01-02,1,1,x\n").encode(), SCHEMA, 1)


def test_epoch_seconds_outside_the_datetime_range_are_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="for TIMESTAMP column ts"):
        parse_csv(
            (HEADER + "1,true,99999999999999999,2024-01-02,1,1,x\n").encode(),
            SCHEMA,
            1,
        )


def test_a_rejected_boolean_stops_the_row_before_a_later_unsupported_value():
    # 'maybe' is rejected in column b, so the bad date in column d is never read: LoadError.
    with pytest.raises(LoadError) as info:
        parse_csv((HEADER + "1,maybe,2024-01-02 03:04:05,2024-02-30,1,1,x\n").encode(), SCHEMA, 1)
    assert 'column_index: 1 column_name: "b"' in info.value.messages[1]


def test_an_unsupported_value_in_any_row_beats_a_rejection_in_another():
    # Row 2 is rejected ('maybe'); row 3 has an unsupported BOOL 'T'. The load is unsupported.
    csv_text = (
        HEADER
        + "1,maybe,2024-01-02 03:04:05,2024-01-02,1,1,x\n2,T,2024-01-02 03:04:05,2024-01-02,1,1,x\n"
    )
    with pytest.raises(CrucibleUnsupportedError, match="CSV value 'T' for BOOL column b"):
        parse_csv(csv_text.encode(), SCHEMA, 1)


# --- rules: schema -------------------------------------------------------------------------------
def test_a_repeated_field_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="CSV load into a ARRAY<STRING> column"):
        parse_csv(b"s\nx\n", [{"name": "s", "type": "STRING", "mode": "REPEATED"}], 1)


def test_a_datetime_column_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="CSV load into a DATETIME column"):
        parse_csv(b"t\n2024-01-02 03:04:05\n", [{"name": "t", "type": "DATETIME"}], 1)


def test_a_record_column_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="CSV load into a STRUCT column"):
        parse_csv(b"r\nx\n", [{"name": "r", "type": "RECORD"}], 1)


def test_a_required_column_is_unsupported():
    # REQUIRED would change which empty fields are errors, and no fixture confirms that.
    with pytest.raises(CrucibleUnsupportedError, match="CSV load into a INT64 column"):
        parse_csv(b"i\n1\n", [{"name": "i", "type": "INTEGER", "mode": "REQUIRED"}], 1)


def test_legacy_and_standard_type_names_are_the_same_column():
    standard = [
        {"name": "i", "type": "INT64"},
        {"name": "b", "type": "BOOL"},
        {"name": "f", "type": "FLOAT64"},
    ]
    legacy = [
        {"name": "i", "type": "INTEGER"},
        {"name": "b", "type": "BOOLEAN"},
        {"name": "f", "type": "FLOAT"},
    ]
    assert parse_csv(b"i,b,f\n1,true,1.5\n", standard, 1) == parse_csv(
        b"i,b,f\n1,true,1.5\n", legacy, 1
    )
