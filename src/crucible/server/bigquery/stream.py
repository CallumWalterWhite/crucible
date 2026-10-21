"""Row values for tabledata.insertAll (streaming inserts).

Each row is a JSON object keyed by column name. A value is accepted only in the spellings below:
the ones google-cloud-bigquery's insert_rows sends (google/cloud/bigquery/_helpers.py,
_SCALAR_VALUE_TO_JSON_ROW) and the plain JSON forms insert_rows_json passes through. record-api
records each one (insert_all_* fixtures); until it does, they are listed in NOT_RECORDED_YET.
Any other spelling raises CrucibleUnsupportedError rather than being guessed. Two bad inputs are
known to fail one row rather than the request, and become that row's insertErrors entry: a
field the table does not have, and an INT64 string that is not a number.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, cast

from crucible.engine import CrucibleUnsupportedError

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
_INTEGER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]{1,9})?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = r"[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
_TIME_RE = re.compile(_TIME)
_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]" + _TIME)
_TIMESTAMP_Z = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T" + _TIME + "Z")  # _RFC3339_MICROS
# str(float) for the non-finite values the client sends (_float_to_json), and BigQuery's names.
_NON_FINITE = {
    "nan": float("nan"),
    "NaN": float("nan"),
    "inf": float("inf"),
    "Infinity": float("inf"),
    "-inf": float("-inf"),
    "-Infinity": float("-inf"),
}


class RowError(Exception):
    """A value BigQuery rejects: the row fails with reason `invalid` at `location`."""

    def __init__(self, location: str, message: str) -> None:
        super().__init__(message)
        self.location = location
        self.message = message


def parse_row(row: Any, fields: list[dict[str, Any]], *, ignore_unknown: bool) -> list[Any]:
    """One insertAll row's `json` object as Python values in the order of `fields`."""
    if not isinstance(row, dict):
        raise CrucibleUnsupportedError("insertAll row json that is not an object")
    values = cast("dict[str, Any]", row)
    by_name = {f["name"].lower(): f for f in fields}
    for key in values:
        if key.lower() not in by_name and not ignore_unknown:
            raise RowError(key, f"no such field: {key}.")
    lowered = {k.lower(): v for k, v in values.items()}
    return [_field_value(lowered.get(f["name"].lower(), _MISSING), f, f["name"]) for f in fields]


_MISSING = object()


def _field_value(value: Any, field: dict[str, Any], location: str) -> Any:
    if field.get("mode") == "REPEATED":
        if value is _MISSING:
            return []  # shortcut: an omitted repeated field is an empty array (to be recorded)
        if not isinstance(value, list):
            raise CrucibleUnsupportedError(f"insertAll value for repeated field {location}")
        items = cast("list[Any]", value)
        if any(item is None for item in items):
            raise CrucibleUnsupportedError(f"insertAll NULL element in repeated field {location}")
        return [_scalar_or_record(item, field, location) for item in items]
    if value is _MISSING or value is None:
        return None
    return _scalar_or_record(value, field, location)


def _scalar_or_record(value: Any, field: dict[str, Any], location: str) -> Any:
    kind = str(field.get("type", "")).upper()
    if kind == "RECORD":
        if not isinstance(value, dict):
            raise CrucibleUnsupportedError(f"insertAll value for RECORD field {location}")
        record = cast("dict[str, Any]", value)
        inner: list[dict[str, Any]] = field.get("fields", [])
        names = {f["name"].lower() for f in inner}
        unknown = [k for k in record if k.lower() not in names]
        if unknown:
            raise CrucibleUnsupportedError(f"insertAll unknown field {location}.{unknown[0]}")
        lowered = {k.lower(): v for k, v in record.items()}
        return {
            f["name"]: _field_value(
                lowered.get(f["name"].lower(), _MISSING), f, f"{location}.{f['name']}"
            )
            for f in inner
        }
    return scalar(kind, value, location)


def scalar(kind: str, value: Any, location: str) -> Any:
    """One scalar value of a legacy-named type (INTEGER, FLOAT, ...) in a confirmed spelling."""
    parse = _SCALARS.get(kind)
    if parse is None:
        raise CrucibleUnsupportedError(f"insertAll into a {kind} column")
    result = parse(value, location)
    if result is _UNCONFIRMED:
        raise CrucibleUnsupportedError(f"insertAll {kind} value {json.dumps(value)} for {location}")
    return result


_UNCONFIRMED = object()


def _int64(value: Any, location: str) -> Any:
    if isinstance(value, bool):
        return _UNCONFIRMED
    if isinstance(value, str):
        if not _INTEGER.fullmatch(value):
            if re.fullmatch(r"[A-Za-z]+", value):
                raise RowError(location, f"Cannot convert value to integer (bad value): {value}")
            return _UNCONFIRMED
        value = int(value)
    if not isinstance(value, int) or not INT64_MIN <= value <= INT64_MAX:
        return _UNCONFIRMED
    return value


def _float64(value: Any, location: str) -> Any:
    if isinstance(value, bool):
        return _UNCONFIRMED
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value in _NON_FINITE:
        return _NON_FINITE[value]
    return _UNCONFIRMED


def _numeric(value: Any, location: str) -> Any:
    if isinstance(value, bool):
        return _UNCONFIRMED
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        number = Decimal(value)
        if abs(number) < Decimal(10) ** 29:
            return number
    return _UNCONFIRMED


def _bool(value: Any, location: str) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"true": True, "false": False}.get(value, _UNCONFIRMED)
    return _UNCONFIRMED


def _string(value: Any, location: str) -> Any:
    return value if isinstance(value, str) else _UNCONFIRMED


def _bytes(value: Any, location: str) -> Any:
    if not isinstance(value, str):
        return _UNCONFIRMED
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error:
        return _UNCONFIRMED


def _date(value: Any, location: str) -> Any:
    if isinstance(value, str) and _DATE.fullmatch(value):
        return date.fromisoformat(value)
    return _UNCONFIRMED


def _datetime(value: Any, location: str) -> Any:
    if isinstance(value, str) and _DATETIME.fullmatch(value):
        return datetime.fromisoformat(value.replace(" ", "T"))
    return _UNCONFIRMED


def _time(value: Any, location: str) -> Any:
    if isinstance(value, str) and _TIME_RE.fullmatch(value):
        return time.fromisoformat(value)
    return _UNCONFIRMED


def _timestamp(value: Any, location: str) -> Any:
    if isinstance(value, str) and _TIMESTAMP_Z.fullmatch(value):
        return datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)  # epoch seconds (insert_rows_json callers)
    return _UNCONFIRMED


def _json(value: Any, location: str) -> Any:
    if not isinstance(value, str):
        return _UNCONFIRMED  # the client sends json.dumps(value) (_json_to_json)
    try:
        json.loads(value)
    except ValueError:
        return _UNCONFIRMED
    return value


_SCALARS = {
    "INTEGER": _int64,
    "FLOAT": _float64,
    "NUMERIC": _numeric,
    "BOOLEAN": _bool,
    "STRING": _string,
    "BYTES": _bytes,
    "DATE": _date,
    "DATETIME": _datetime,
    "TIME": _time,
    "TIMESTAMP": _timestamp,
    "JSON": _json,
}
