"""CSV load parsing (server/bigquery/load.py) against the recorded BigQuery load fixtures.

The data-driven test reads every load case by name from tests/fixtures/bigquery_api: the CSV is the
upload's request body, the schema and skip count are the load job's configuration, and the outcome
is the job's recorded errors (LoadError) or the query results of the loaded table. The edge tests
below use inline CSV for the rules the fixtures do not single out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from crucible.engine import CrucibleUnsupportedError
from crucible.server.bigquery.encoding import encode_value, warehouse_type_from_field
from crucible.server.bigquery.load import LoadError, parse_csv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bigquery_api"

# The load schema every inline case uses: the recorded baseline schema, in order.
SCHEMA: list[dict[str, str]] = [
    {"name": "id", "type": "INTEGER"},
    {"name": "b", "type": "BOOLEAN"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "d", "type": "DATE"},
    {"name": "n", "type": "NUMERIC"},
    {"name": "f", "type": "FLOAT"},
    {"name": "s", "type": "STRING"},
]
HEADER = "id,b,ts,d,n,f,s\n"
GOOD_ROW = "1,true,2024-01-02 03:04:05,2024-01-02,1,1,x\n"
VALUES = {
    "id": "1",
    "b": "true",
    "ts": "2024-01-02 03:04:05",
    "d": "2024-01-02",
    "n": "1",
    "f": "1",
    "s": "x",
}


def fixture(path: Path) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


def case_fixture(case: str, kind: str) -> dict[str, Any]:
    """The recorded `kind` (upload, jobs_get, query_results) of one load case, found by name."""
    [path] = FIXTURES.glob(f"*_load_{case}_{kind}.json")
    return fixture(path)


def load_case_names() -> list[str]:
    return sorted(
        {
            path.name.split("_load_", 1)[1].removesuffix("_upload.json")
            for path in FIXTURES.glob("*_load_*_upload.json")
        }
    )


LOAD_CASES = load_case_names()


def data_row(column: str, text: str) -> str:
    """A data line: the GOOD_ROW values with one column's text replaced."""
    return ",".join(dict(VALUES, **{column: text}).values()) + "\n"


def row_with(column: str, text: str) -> bytes:
    return (HEADER + data_row(column, text)).encode()


# --- every recorded load case ---------------------------------------------------------------------
def test_the_recording_has_at_least_27_load_cases():
    assert len(LOAD_CASES) >= 27


@pytest.mark.parametrize("case", LOAD_CASES)
def test_a_recorded_load_case_parses_as_the_recording_shows(case):
    csv_text = case_fixture(case, "upload")["request"]["body"]
    job = case_fixture(case, "jobs_get")["body"]
    load = job["configuration"]["load"]
    fields = load["schema"]["fields"]
    skip = load.get("skipLeadingRows", 0)
    errors = job["status"].get("errors")
    if errors:
        with pytest.raises(LoadError) as info:
            parse_csv(csv_text.encode(), fields, skip)
        assert info.value.messages == [error["message"] for error in errors]
        return
    rows = parse_csv(csv_text.encode(), fields, skip)
    types = [warehouse_type_from_field(field) for field in fields]
    encoded = [
        [encode_value(value, kind) for value, kind in zip(row, types, strict=True)] for row in rows
    ]
    recorded = case_fixture(case, "query_results")["body"]["rows"]
    assert encoded == [[cell["v"] for cell in row["f"]] for row in recorded]


# --- spellings no fixture records: unsupported, not guessed ---------------------------------------
@pytest.mark.parametrize(
    ("column", "text", "kind"),
    [
        ("b", "f", "BOOL"),
        ("b", "no", "BOOL"),
        ("b", "0", "BOOL"),
        ("b", "FALSE", "BOOL"),
        ("b", "Yes", "BOOL"),
        ("b", "t", "BOOL"),
        ("f", "inf", "FLOAT64"),
        ("f", "nan", "FLOAT64"),
        ("f", "Infinity", "FLOAT64"),
        ("f", "+1.5", "FLOAT64"),
        ("f", "1e3", "FLOAT64"),
        ("n", "-1e3", "NUMERIC"),
        ("n", "1E3", "NUMERIC"),
        ("d", "2024-01-2", "DATE"),
        ("ts", "2024-01-02 03:04:05.12", "TIMESTAMP"),
        ("ts", "2024-01-02T03:04:05.123", "TIMESTAMP"),
        ("f", '"1.5"', "FLOAT64"),
        ("d", '"2024-01-02"', "DATE"),
        ("id", '""', "INT64"),
    ],
)
def test_an_unrecorded_spelling_is_unsupported(column, text, kind):
    with pytest.raises(CrucibleUnsupportedError, match=f"for {kind} column {column}"):
        parse_csv(row_with(column, text), SCHEMA, 1)


# --- rules: the file ------------------------------------------------------------------------------
def test_a_byte_order_mark_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="byte order mark"):
        parse_csv(b"\xef\xbb\xbf" + HEADER.encode() + GOOD_ROW.encode(), SCHEMA, 1)


def test_a_lone_carriage_return_is_unsupported():
    csv_text = HEADER + GOOD_ROW + "2,true,2024-01-02 03:04:05,2024-01-02,1,1,y\r"
    with pytest.raises(CrucibleUnsupportedError, match="carriage return"):
        parse_csv(csv_text.encode(), SCHEMA, 1)


def test_a_blank_line_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="blank line"):
        parse_csv((HEADER + "\n").encode(), SCHEMA, 1)


def test_a_wrong_column_count_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="fields for 7 columns"):
        parse_csv((HEADER + "1,true,2024-01-02\n").encode(), SCHEMA, 1)
    with pytest.raises(CrucibleUnsupportedError, match="fields for 7 columns"):
        parse_csv((HEADER + "1,true,2024-01-02,2024-01-02,1,1,x,extra\n").encode(), SCHEMA, 1)


def test_a_file_without_a_final_newline_loads():
    assert parse_csv((HEADER + GOOD_ROW.rstrip("\n")).encode(), SCHEMA, 1)


def test_skipping_leading_rows_is_by_count():
    assert len(parse_csv(("junk\n" + HEADER + GOOD_ROW).encode(), SCHEMA, 2)) == 1


def test_the_header_as_data_is_unsupported():
    # skip_leading_rows=0 reads the header as a row; "id" is not an INTEGER.
    with pytest.raises(CrucibleUnsupportedError, match="CSV value 'id' for INT64 column id"):
        parse_csv((HEADER + GOOD_ROW).encode(), SCHEMA, 0)


def test_a_negative_skip_is_an_error():
    with pytest.raises(ValueError, match="skip_leading_rows"):
        parse_csv((HEADER + GOOD_ROW).encode(), SCHEMA, -1)


def test_a_quote_inside_an_unquoted_field_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="unquoted field"):
        parse_csv((HEADER + '1,true,2024-01-02,2024-01-02,1,1,a"b\n').encode(), SCHEMA, 1)


def test_text_after_a_closing_quote_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="after a closing quote"):
        parse_csv((HEADER + '1,true,2024-01-02,2024-01-02,1,1,"a"b\n').encode(), SCHEMA, 1)


def test_an_unclosed_quote_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="without a close"):
        parse_csv((HEADER + '1,true,2024-01-02,2024-01-02,1,1,"a\n').encode(), SCHEMA, 1)


def test_a_field_that_is_not_utf_8_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="not UTF-8"):
        parse_csv(HEADER.encode() + b"1,true,2024-01-02,2024-01-02,1,1,\xff\n", SCHEMA, 1)


# --- rules: values --------------------------------------------------------------------------------
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


def test_offset_is_converted_to_utc():
    # 03:04:05+01:00 is 02:04:05 UTC.
    [row] = parse_csv(
        (HEADER + "1,true,2024-01-02 03:04:05+01:00,2024-01-02,1,1,x\n").encode(), SCHEMA, 1
    )
    assert row[2] == datetime(2024, 1, 2, 2, 4, 5, tzinfo=UTC)


def test_a_negative_offset_is_added():
    [row] = parse_csv(
        (HEADER + "1,true,2024-01-02 03:04:05-05:00,2024-01-02,1,1,x\n").encode(), SCHEMA, 1
    )
    assert row[2] == datetime(2024, 1, 2, 8, 4, 5, tzinfo=UTC)


def test_epoch_seconds_outside_the_datetime_range_are_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="for TIMESTAMP column ts"):
        parse_csv(row_with("ts", "99999999999999999"), SCHEMA, 1)


def test_an_integer_out_of_range_is_unsupported():
    with pytest.raises(CrucibleUnsupportedError, match="out of INT64 range"):
        parse_csv(row_with("id", "9223372036854775808"), SCHEMA, 1)


def test_the_largest_integer_loads():
    [row] = parse_csv(row_with("id", "9223372036854775807"), SCHEMA, 1)
    assert row[0] == 2**63 - 1


def test_numeric_with_nine_fraction_digits_loads_exactly():
    [row] = parse_csv(row_with("n", "-0.000000001"), SCHEMA, 1)
    assert row[4] == Decimal("-0.000000001")


# --- rules: rejections (LoadError) ----------------------------------------------------------------
def test_the_summary_counts_every_data_row_and_the_rejected_ones():
    bad = "2,maybe,2024-01-02 03:04:05,2024-01-02,1,1,x\n"
    with pytest.raises(LoadError) as info:
        parse_csv((HEADER + GOOD_ROW + bad + GOOD_ROW).encode(), SCHEMA, 1)
    assert info.value.messages[0].endswith("Rows: 3; errors: 1; max bad: 0; error percent: 0")
    offset = len((HEADER + GOOD_ROW).encode())
    assert f"line_number: 3 byte_offset_to_start_of_line: {offset} " in info.value.messages[1]
    assert (
        'column_index: 1 column_name: "b" column_type: BOOL value: "maybe"'
        in (info.value.messages[1])
    )


def test_the_first_failing_column_wins_and_stops_the_row():
    # 'maybe' is rejected in column b, so the bad date in column d is never read.
    with pytest.raises(LoadError) as info:
        parse_csv((HEADER + "1,maybe,2024-01-02 03:04:05,2024-02-30,1,1,x\n").encode(), SCHEMA, 1)
    assert 'column_index: 1 column_name: "b"' in info.value.messages[1]


def test_a_rejected_value_stops_the_row_before_a_later_unsupported_value():
    # 'maybe' is rejected in column b, so the unrecorded FLOAT64 'inf' in column f is never read.
    with pytest.raises(LoadError) as info:
        parse_csv((HEADER + "1,maybe,2024-01-02 03:04:05,2024-01-02,1,inf,x\n").encode(), SCHEMA, 1)
    assert 'column_index: 1 column_name: "b"' in info.value.messages[1]


def test_an_unsupported_value_in_any_row_beats_a_rejection_in_another():
    # Row 2 is rejected ('maybe'); row 3 has the unrecorded BOOL 'f'. The load is unsupported.
    csv_text = HEADER + data_row("b", "maybe") + data_row("b", "f")
    with pytest.raises(CrucibleUnsupportedError, match="CSV value 'f' for BOOL column b"):
        parse_csv(csv_text.encode(), SCHEMA, 1)


# --- rules: schema --------------------------------------------------------------------------------
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
