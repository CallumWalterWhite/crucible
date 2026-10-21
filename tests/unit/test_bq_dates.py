"""TIMESTAMP_DIFF, EXTRACT, FORMAT_*, PARSE_TIMESTAMP, TIMESTAMP/STRING with a zone, CURRENT_*
(transforms/dates.py).

Expected values come from Python (`datetime`, `zoneinfo`, `date.isocalendar`), not from DuckDB, so a
wrong DuckDB answer cannot agree with its own expectation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import column_types, values

LA = "America/Los_Angeles"


def canonical_utc(moment: datetime) -> str:
    """A TIMESTAMP as the corpus writes it: UTC, with a fraction only when non-zero (ADR 0014)."""
    moment = moment.astimezone(UTC)
    fraction = f".{moment.microsecond:06d}" if moment.microsecond else ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


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


# --- EXTRACT(ISOWEEK) and EXTRACT(ISOYEAR): ISO 8601, as date.isocalendar() defines it ---------
@pytest.mark.parametrize("day", [date(2024, 1, 1), date(2024, 12, 30), date(2027, 1, 1)])
def test_isoweek_and_isoyear_match_iso_8601(bq, day):
    week = bq(f"SELECT EXTRACT(ISOWEEK FROM DATE '{day.isoformat()}') AS v")
    year = bq(f"SELECT EXTRACT(ISOYEAR FROM DATE '{day.isoformat()}') AS v")
    iso_year, iso_week, _ = day.isocalendar()
    assert column_types(week) == [("v", "INT64")]
    assert values(week) == [[iso_week]]
    assert column_types(year) == [("v", "INT64")]
    assert values(year) == [[iso_year]]


def test_isoweek_and_isoyear_of_the_year_boundary_cases(bq):
    # 2024-12-30 is ISO week 1 of 2025; 2027-01-01 is ISO week 53 of 2026.
    assert values(bq("SELECT EXTRACT(ISOWEEK FROM DATE '2024-12-30') AS v")) == [[1]]
    assert values(bq("SELECT EXTRACT(ISOYEAR FROM DATE '2024-12-30') AS v")) == [[2025]]
    assert values(bq("SELECT EXTRACT(ISOWEEK FROM DATE '2027-01-01') AS v")) == [[53]]
    assert values(bq("SELECT EXTRACT(ISOYEAR FROM DATE '2027-01-01') AS v")) == [[2026]]


def test_isoweek_null_is_null(bq):
    assert values(bq("SELECT EXTRACT(ISOWEEK FROM CAST(NULL AS DATE)) AS v")) == [[None]]


# --- EXTRACT(WEEK): Sunday-start weeks, days before the first Sunday are week 0 ----------------
@pytest.mark.parametrize(
    ("day", "week"),
    [("2024-01-07", 1), ("2024-01-06", 0), ("2023-01-01", 1), ("2024-12-31", 52), (None, None)],
)
def test_week_starts_on_sunday(bq, day, week):
    value = f"DATE '{day}'" if day else "CAST(NULL AS DATE)"
    assert values(bq(f"SELECT EXTRACT(WEEK FROM {value}) AS v")) == [[week]]


@pytest.mark.parametrize(
    ("day", "week"),
    [("SUNDAY", 9), ("MONDAY", 10), ("WEDNESDAY", 10), ("THURSDAY", 9), ("SATURDAY", 9)],
)
def test_week_with_a_weekday_counts_weeks_from_that_day(bq, day, week):
    # 2024-03-06 is a Wednesday; 2024-01-01 a Monday. Days before the first <day> are week 0.
    assert values(bq(f"SELECT EXTRACT(WEEK({day}) FROM DATE '2024-03-06') AS v")) == [[week]]
    assert values(bq(f"SELECT EXTRACT(WEEK({day}) FROM DATE '2024-01-01') AS v")) == [
        [1 if day == "MONDAY" else 0]
    ]


# --- FORMAT_DATE / FORMAT_DATETIME / FORMAT_TIMESTAMP: `%-` elements are refused -----------------
def test_format_without_a_dash_element_is_unchanged(bq):
    assert values(bq("SELECT FORMAT_DATE('%Y-%m', DATE '2024-03-05') AS v")) == [["2024-03"]]


def test_format_null_value_is_null(bq):
    assert values(bq("SELECT FORMAT_DATE('%Y', CAST(NULL AS DATE)) AS v")) == [[None]]


@pytest.mark.parametrize(
    "sql",
    [
        # SQLGlot turns %e into %-d, so %e is refused whatever its origin (transforms/dates.py).
        "SELECT FORMAT_DATE('%b %e %Y', DATE '2024-03-05') AS v",
        "SELECT FORMAT_DATE('%e', DATE '2024-03-15') AS v",
        "SELECT FORMAT_TIMESTAMP('%e %H', TIMESTAMP '2024-03-05 10:00:00+00') AS v",
        "SELECT FORMAT_DATETIME('%e', DATETIME '2024-03-05 10:00:00') AS v",
        # A literal %-d is the same after conversion, so it is refused too.
        "SELECT FORMAT_DATE('%-d', DATE '2024-03-05') AS v",
        # SQLGlot turns BigQuery's %%e into %%-d (prints %-d, where BigQuery prints e). Refused.
        "SELECT FORMAT_DATE('%%e', DATE '2024-03-05') AS v",
        "SELECT FORMAT_DATE('%%%e', DATE '2024-03-05') AS v",
    ],
)
def test_percent_dash_elements_are_refused(bq, sql):
    with pytest.raises(CrucibleUnsupportedError, match="format element %-"):
        bq(sql)


def test_other_percent_dash_elements_are_refused(bq):
    with pytest.raises(CrucibleUnsupportedError, match="format element %-x"):
        bq("SELECT FORMAT_DATE('%-x', DATE '2024-03-05') AS v")


# --- Time zones: the argument is applied exactly, or refused (never dropped) ------------------
@pytest.mark.parametrize(
    "instant",
    [
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),  # the previous day in Los Angeles
        datetime(2024, 3, 10, 9, 30, tzinfo=UTC),  # just before the spring-forward switch
        datetime(2024, 3, 10, 11, 0, tzinfo=UTC),  # after it
    ],
)
def test_format_timestamp_applies_the_zone(bq, instant):
    literal = instant.strftime("%Y-%m-%d %H:%M:%S+00")
    result = bq(f"SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', TIMESTAMP '{literal}', '{LA}') AS v")
    expected = instant.astimezone(ZoneInfo(LA)).strftime("%Y-%m-%d %H:%M")
    assert column_types(result) == [("v", "STRING")]
    assert values(result) == [[expected]]


def test_format_date_and_datetime_with_a_third_argument_are_invalid_queries(bq):
    for sql in [
        f"SELECT FORMAT_DATE('%Y', DATE '2024-01-01', '{LA}') AS v",
        f"SELECT FORMAT_DATETIME('%Y', DATETIME '2024-01-01 02:00:00', '{LA}') AS v",
    ]:
        with pytest.raises(CrucibleError) as info:
            bq(sql)
        assert info.value.kind is ErrorKind.INVALID_QUERY


@pytest.mark.parametrize("element", ["%Z", "%z"])
def test_zone_names_and_offsets_with_a_zone_are_unsupported(bq, element):
    with pytest.raises(CrucibleUnsupportedError, match="with a time zone argument"):
        bq(f"SELECT FORMAT_TIMESTAMP('{element}', TIMESTAMP '2024-01-01 02:00:00+00', '{LA}') AS v")


def test_parse_timestamp_reads_the_wall_clock_in_the_zone(bq):
    # '02' on 2024-01-01 in Los Angeles is 10:00 UTC, not 02:00 UTC.
    result = bq(f"SELECT PARSE_TIMESTAMP('%Y-%m-%d %H', '2024-01-01 02', '{LA}') AS v")
    assert column_types(result) == [("v", "TIMESTAMP")]
    local = datetime(2024, 1, 1, 2, tzinfo=ZoneInfo(LA))
    assert values(result) == [[canonical_utc(local)]]


def test_parse_timestamp_with_an_offset_element_and_a_zone_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match="with a time zone argument"):
        bq(f"SELECT PARSE_TIMESTAMP('%Y %z', '2024 +0000', '{LA}') AS v")


def test_timestamp_of_a_plain_string_applies_the_zone(bq):
    result = bq(f"SELECT TIMESTAMP('2024-01-01 02:00:00', '{LA}') AS v")
    local = datetime(2024, 1, 1, 2, tzinfo=ZoneInfo(LA))
    assert values(result) == [[canonical_utc(local)]]


@pytest.mark.parametrize("text", ["2024-01-01 02:00:00+05", "2024-01-01T02:00:00Z"])
def test_timestamp_of_a_string_with_an_offset_is_unsupported_at_run_time(bq, text):
    with pytest.raises(CrucibleUnsupportedError, match="offset or zone name"):
        bq(f"SELECT TIMESTAMP('{text}', '{LA}') AS v")


def test_timestamp_null_string_is_null(bq):
    assert values(bq(f"SELECT TIMESTAMP(CAST(NULL AS STRING), '{LA}') AS v")) == [[None]]


def test_string_of_a_timestamp_with_a_zone_is_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match="STRING\\(timestamp, time_zone\\)"):
        bq(f"SELECT STRING(TIMESTAMP '2024-01-01 02:00:00+00', '{LA}') AS v")


def _midnight_utc(local: datetime) -> str:
    return canonical_utc(local.replace(hour=0, minute=0, second=0, microsecond=0))


# Each conversion, with its canonical value (ADR 0014) computed from the zoneinfo local time.
ZONE_CONVERSIONS = [
    ("DATE(TIMESTAMP '{ts}', '{tz}')", lambda local: local.date().isoformat()),
    ("DATETIME(TIMESTAMP '{ts}', '{tz}')", lambda local: local.replace(tzinfo=None).isoformat()),
    ("TIME(TIMESTAMP '{ts}', '{tz}')", lambda local: local.time().isoformat()),
    ("EXTRACT(DAY FROM TIMESTAMP '{ts}' AT TIME ZONE '{tz}')", lambda local: local.day),
    ("TIMESTAMP_TRUNC(TIMESTAMP '{ts}', DAY, '{tz}')", _midnight_utc),
]


@pytest.mark.parametrize(("sql", "expected"), ZONE_CONVERSIONS)
@pytest.mark.parametrize(
    "instant",
    [
        datetime(2024, 3, 10, 9, 30, tzinfo=UTC),  # before the spring-forward switch
        datetime(2024, 3, 10, 11, 0, tzinfo=UTC),  # after it
        datetime(2024, 1, 2, 3, 15, 30, 123456, tzinfo=UTC),
    ],
)
def test_zone_conversions_match_zoneinfo(bq, sql, expected, instant):
    query = sql.format(ts=instant.strftime("%Y-%m-%d %H:%M:%S.%f+00"), tz=LA)
    local = instant.astimezone(ZoneInfo(LA))
    assert values(bq(f"SELECT {query} AS v")) == [[expected(local)]]


def test_current_datetime_is_now_in_the_zone(bq):
    before = datetime.now(UTC)
    local = values(bq(f"SELECT CURRENT_DATETIME('{LA}') AS v"))[0][0]
    after = datetime.now(UTC)
    parsed = datetime.fromisoformat(str(local)).replace(tzinfo=ZoneInfo(LA))
    assert before - timedelta(minutes=1) <= parsed.astimezone(UTC) <= after + timedelta(minutes=1)


def test_current_datetime_without_a_zone_is_utc(bq):
    before = datetime.now(UTC).replace(tzinfo=None)
    local = str(values(bq("SELECT CURRENT_DATETIME() AS v"))[0][0])
    parsed = datetime.fromisoformat(local)
    assert abs(parsed - before) < timedelta(minutes=1)


def test_current_date_with_a_zone_is_the_local_date(bq):
    today = datetime.now(ZoneInfo(LA)).date()
    result = values(bq(f"SELECT CURRENT_DATE('{LA}') AS v"))
    assert result[0][0] in (str(today), today.isoformat())
