"""CSV load parsing for BigQuery load jobs: the rules the recorded fixtures confirm (Phase 3, B2).

Each rule cites the recorded fixture that proves it (tests/fixtures/bigquery_api); the CSV strings
are in tests/unit/test_bq_load_csv.py, copied from the commit that recorded them. A construct the
fixtures do not confirm raises CrucibleUnsupportedError rather than being guessed. Two outcomes are
kept apart, as BigQuery keeps them apart:

- rejected: BigQuery refuses a row. The load fails with a LoadError listing each rejected row.
- unsupported: Crucible cannot say what BigQuery would load. Any unsupported value fails the load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from crucible.engine import CrucibleUnsupportedError

INT64_MAX = 2**63 - 1
NUMERIC_MAX_FRACTION_DIGITS = 9

# Plain decimal spelling for NUMERIC and FLOAT (load_baseline_query_results); NUMERIC also takes
# 1e3, FLOAT64 also -inf and NaN (see below).
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_INTEGER = re.compile(r"[+-]?[0-9]+")  # "-1" and "+1" load (recorded)
_NUMERIC_EXPONENT = re.compile(r"[0-9]+e[0-9]+")  # "1e3" loads as 1000 (recorded)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATE_ONE_DIGIT = re.compile(r"[0-9]{4}-[0-9]-[0-9]")  # "2024-1-2" loads (recorded)
_DATE_SLASHES = re.compile(r"[0-9]{4}/[0-9]{2}/[0-9]{2}")  # rejected as a DATE (recorded)
_EPOCH = re.compile(r"[0-9]+")
# YYYY-MM-DD HH:MM:SS, with the forms the fixtures confirm as suffixes or offsets.
_TS_BASE = r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})"
_TS_PLAIN = re.compile(_TS_BASE)
_TS_FRACTION = re.compile(_TS_BASE + r"\.([0-9]{6}|[0-9]{3})")  # six or three digits (recorded)
_TS_SLASHES = re.compile(r"([0-9]{4})/([0-9]{2})/([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})")
_TS_T = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})")
_TS_FRACTION_UTC = re.compile(_TS_BASE + r"\.([0-9]{6}) UTC")  # 55
_TS_OFFSET = re.compile(_TS_BASE + r"([+-])([0-9]{2}):([0-9]{2})")  # 60
_TS_T_Z = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")  # 50

# BigQuery's names for the types the fixtures cover; error messages print these.
_STANDARD = {
    "INTEGER": "INT64",
    "INT64": "INT64",
    "BOOLEAN": "BOOL",
    "BOOL": "BOOL",
    "FLOAT": "FLOAT64",
    "FLOAT64": "FLOAT64",
    "NUMERIC": "NUMERIC",
    "TIMESTAMP": "TIMESTAMP",
    "DATE": "DATE",
    "STRING": "STRING",
}
_NOT_A_TIMESTAMP = (
    "Could not parse '{value}' as a timestamp. Required format is YYYY-MM-DD HH:MM[:SS[.SSSSSS]] "
    "or YYYY/MM/DD HH:MM[:SS[.SSSSSS]]"
)
_SUMMARY = (
    "Error while reading data, error message: CSV processing encountered too many errors, giving "
    "up. Rows: {rows}; errors: {errors}; max bad: 0; error percent: 0"
)
_ROW_ERROR = (
    "Error while reading data, error message: {detail}; line_number: {line} "
    'byte_offset_to_start_of_line: {offset} column_index: {index} column_name: "{name}" '
    'column_type: {kind} value: "{value}"'
)


class LoadError(Exception):
    """BigQuery rejected the file. messages[0] is the summary, then one message per failing row."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__(messages[0])
        self.messages = messages


@dataclass(frozen=True)
class _Column:
    name: str
    kind: str  # the standard type name, as BigQuery prints it


@dataclass(frozen=True)
class _Field:
    text: str
    quoted: bool


@dataclass(frozen=True)
class _Line:
    number: int  # 1-based, counting the skipped lines
    offset: int  # byte offset of the line's first byte
    fields: list[_Field]


class _Rejected(Exception):
    """A value BigQuery refuses. `index` is the column, set once the row is known."""

    def __init__(self, detail: str, index: int = -1) -> None:
        super().__init__(detail)
        self.detail = detail
        self.index = index


class _NotConfirmed(Exception):
    """A spelling no fixture confirms: unsupported, not rejected."""


def parse_csv(
    data: bytes, fields: list[dict[str, Any]], skip_leading_rows: int
) -> list[tuple[Any, ...]]:
    """Rows of Python values in schema order, or raise LoadError / CrucibleUnsupportedError."""
    if skip_leading_rows < 0:
        raise ValueError("skip_leading_rows must be >= 0")
    columns = [_column(field) for field in fields]
    lines = _tokenize(data, columns, skip_leading_rows)
    rows: list[tuple[Any, ...]] = []
    failures: list[str] = []
    for line in lines:
        try:
            rows.append(_row(line, columns))
        except _Rejected as exc:
            failures.append(_row_message(line, columns, exc))
    if failures:
        summary = _SUMMARY.format(rows=len(lines), errors=len(failures))
        raise LoadError([summary, *failures])
    return rows


# --- schema --------------------------------------------------------------------------------------
def _column(field: dict[str, Any]) -> _Column:
    name = str(field["name"])
    raw_type = str(field["type"]).upper()
    mode = str(field.get("mode", "NULLABLE")).upper()
    kind = _STANDARD.get(raw_type)
    if mode == "REPEATED":
        raise CrucibleUnsupportedError(f"CSV load into a ARRAY<{kind or raw_type}> column")
    # Only NULLABLE is confirmed: REQUIRED would change which empty fields are errors.
    if kind is None or raw_type in ("RECORD", "STRUCT") or mode != "NULLABLE":
        standard = "STRUCT" if raw_type in ("RECORD", "STRUCT") else (kind or raw_type)
        raise CrucibleUnsupportedError(f"CSV load into a {standard} column")
    return _Column(name=name, kind=kind)


# --- tokenizing ----------------------------------------------------------------------------------
def _tokenize(data: bytes, columns: list[_Column], skip: int) -> list[_Line]:
    """Lines and their fields. Structural problems are unsupported at once."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise CrucibleUnsupportedError("CSV with a byte order mark")
    raw_lines = data.split(b"\n")
    if raw_lines[-1] == b"":
        raw_lines.pop()  # the file ended with a newline; the last line may also lack one
    # CRLF line endings load like LF (recorded); any other carriage return is unconfirmed.
    crlf = b"\r" in data
    if crlf and not all(raw.endswith(b"\r") and raw.count(b"\r") == 1 for raw in raw_lines):
        raise CrucibleUnsupportedError("CSV with a carriage return other than CRLF line endings")
    lines: list[_Line] = []
    offset = 0
    for number, raw in enumerate(raw_lines, start=1):
        start = offset
        offset += len(raw) + 1
        if number <= skip:
            continue
        try:
            text = (raw[:-1] if crlf else raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CrucibleUnsupportedError("CSV that is not UTF-8") from exc
        if text == "":
            raise CrucibleUnsupportedError(f"CSV blank line {number}")
        fields = _split(text, number)
        if len(fields) != len(columns):
            raise CrucibleUnsupportedError(
                f"CSV row with {len(fields)} fields for {len(columns)} columns (line {number})"
            )
        lines.append(_Line(number=number, offset=start, fields=fields))
    return lines


def _split(line: str, number: int) -> list[_Field]:
    """Fields of one line. A quoted field may hold commas; a quote anywhere else is unsupported."""
    fields: list[_Field] = []
    pos = 0
    end = len(line)
    while True:
        if pos < end and line[pos] == '"':
            parts: list[str] = []
            close = pos
            while True:  # "" inside quotes is one quote: "a""b" loads as a"b (recorded)
                start, close = close + 1, line.find('"', close + 1)
                if close < 0:
                    raise CrucibleUnsupportedError(
                        f"CSV quoted field without a close (line {number})"
                    )
                parts.append(line[start:close])
                if close + 1 < end and line[close + 1] == '"':
                    parts.append('"')
                    close += 1
                    continue
                break
            if close + 1 < end and line[close + 1] != ",":
                raise CrucibleUnsupportedError(f"CSV text after a closing quote (line {number})")
            fields.append(_Field(text="".join(parts), quoted=True))
            pos = close + 1
        else:
            comma = line.find(",", pos)
            stop = end if comma < 0 else comma
            text = line[pos:stop]
            if '"' in text:
                raise CrucibleUnsupportedError(
                    f"CSV quote inside an unquoted field (line {number})"
                )
            fields.append(_Field(text=text, quoted=False))
            pos = stop
        if pos == end:
            return fields
        pos += 1  # the comma


# --- values --------------------------------------------------------------------------------------
def _row(line: _Line, columns: list[_Column]) -> tuple[Any, ...]:
    """A row's values, stopping at the first column BigQuery would reject.

    An unsupported value raises at once, before any later column is read. A rejection stops the
    row too, so a later column that would be unsupported is never looked at, as BigQuery does.
    """
    values: list[Any] = []
    for index, (field, column) in enumerate(zip(line.fields, columns, strict=True)):
        try:
            values.append(_value(field, column, line.number))
        except _Rejected as exc:
            raise _Rejected(exc.detail, index) from None
    return tuple(values)


def _value(field: _Field, column: _Column, number: int) -> Any:
    if field.text == "":
        if field.quoted and column.kind == "STRING":
            return ""  # a quoted empty STRING is '' (recorded)
        if field.quoted:
            raise CrucibleUnsupportedError(
                f"CSV quoted empty value for {column.kind} column {column.name} (line {number})"
            )
        return None  # an unquoted empty field is NULL for every type (load_empty_fields)
    if field.quoted and column.kind not in ("STRING", "INT64"):  # "1" loads as INT64 1
        raise CrucibleUnsupportedError(
            f"CSV quoted value '{field.text}' for {column.kind} column {column.name} "
            f"(line {number})"
        )
    try:
        return _PARSERS[column.kind](field.text)
    except _NotConfirmed:
        raise CrucibleUnsupportedError(
            f"CSV value '{field.text}' for {column.kind} column {column.name} (line {number})"
        ) from None


def _parse_int(text: str) -> int:
    if not _INTEGER.fullmatch(text):
        raise _NotConfirmed
    value = int(text)
    if value > INT64_MAX:
        raise CrucibleUnsupportedError(f"CSV value '{text}' is out of INT64 range")
    return value


# Exactly the spellings recorded; others (f, no, 0, FALSE, Yes, ...) are not confirmed.
_BOOLS = {"true": True, "false": False, "T": True, "1": True, "yes": True, "TRUE": True}
_BOOLS["False"] = False


def _parse_bool(text: str) -> bool:
    if text in _BOOLS:
        return _BOOLS[text]
    if (
        text == "maybe"
    ):  # fixture load_bad_value_jobs_get: the one spelling BigQuery is confirmed to reject
        raise _Rejected("Unable to parse")
    raise _NotConfirmed


def _parse_float(text: str) -> float:
    if text in ("-inf", "NaN"):  # recorded: -Infinity and NaN
        return float(text)
    if not _DECIMAL.fullmatch(text):
        raise _NotConfirmed
    return float(text)


def _parse_numeric(text: str) -> Decimal:
    if _NUMERIC_EXPONENT.fullmatch(text):
        # Plain digits: DuckDB reads Decimal('1E+3') as 1.
        return Decimal(format(Decimal(text), "f"))
    if not _DECIMAL.fullmatch(text):
        raise _NotConfirmed
    if len(text.partition(".")[2]) > NUMERIC_MAX_FRACTION_DIGITS:
        raise _Rejected(f"Invalid NUMERIC value: {text}")  # recorded for ten digits
    return Decimal(text)


def _parse_date(text: str) -> date:
    if _DATE_SLASHES.fullmatch(text):
        raise _Rejected("Unable to parse")
    if _DATE_ONE_DIGIT.fullmatch(text):
        year, month, day = (int(p) for p in text.split("-"))
        return date(year, month, day)
    if not _DATE.fullmatch(text):
        raise _NotConfirmed
    try:
        return date.fromisoformat(text)
    except ValueError:  # not a calendar date, e.g. 2024-02-30 (recorded)
        raise _Rejected("Unable to parse") from None


def _parse_string(text: str) -> str:
    return text


def _parse_timestamp(text: str) -> datetime:
    if _DATE.fullmatch(text):
        # A date alone is rejected (load_ts_date_only_jobs_get).
        raise _Rejected(_NOT_A_TIMESTAMP.format(value=text))
    if _EPOCH.fullmatch(text):
        try:
            return datetime.fromtimestamp(int(text), tz=UTC)  # 65: epoch seconds
        except (OverflowError, OSError, ValueError) as exc:
            raise _NotConfirmed from exc
    try:
        return _timestamp_fields(text)
    except ValueError as exc:
        raise _NotConfirmed from exc


def _timestamp_fields(text: str) -> datetime:
    """The confirmed datetime spellings, as UTC. Raises ValueError for an impossible date."""
    if match := _TS_T_Z.fullmatch(text):  # 50
        return _from_groups(match, 0).replace(tzinfo=UTC)
    if match := _TS_FRACTION.fullmatch(text) or _TS_FRACTION_UTC.fullmatch(text):
        micros = int(match.group(7).ljust(6, "0"))
        return _from_groups(match, micros).replace(tzinfo=UTC)
    if match := _TS_T.fullmatch(text) or _TS_SLASHES.fullmatch(text):  # recorded, read as UTC
        return _from_groups(match, 0).replace(tzinfo=UTC)
    if match := _TS_OFFSET.fullmatch(text):  # 60: local time minus the offset is UTC
        local = _from_groups(match, 0)
        shift = timedelta(hours=int(match.group(8)), minutes=int(match.group(9)))
        return (local - shift if match.group(7) == "+" else local + shift).replace(tzinfo=UTC)
    if match := _TS_PLAIN.fullmatch(text):  # 25
        return _from_groups(match, 0).replace(tzinfo=UTC)
    raise _NotConfirmed


def _from_groups(match: re.Match[str], fraction: int) -> datetime:
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    return datetime(year, month, day, hour, minute, second, fraction)


_PARSERS: dict[str, Any] = {
    "INT64": _parse_int,
    "BOOL": _parse_bool,
    "FLOAT64": _parse_float,
    "NUMERIC": _parse_numeric,
    "TIMESTAMP": _parse_timestamp,
    "DATE": _parse_date,
    "STRING": _parse_string,
}


# --- messages ------------------------------------------------------------------------------------
def _row_message(line: _Line, columns: list[_Column], exc: _Rejected) -> str:
    column = columns[exc.index]
    return _ROW_ERROR.format(
        detail=exc.detail,
        line=line.number,
        offset=line.offset,
        index=exc.index,
        name=column.name,
        kind=column.kind,
        value=line.fields[exc.index].text,
    )
