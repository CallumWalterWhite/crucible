"""Query parameters (`queryParameters`, `parameterMode`) on jobs.query and jobs.insert.

REST shape: QueryParameter {name, parameterType {type, arrayType, structTypes}, parameterValue
{value, arrayValues, structValues}} (https://cloud.google.com/bigquery/docs/reference/rest/v2/QueryParameter).
Scalar values are accepted in the spellings google-cloud-bigquery sends
(google/cloud/bigquery/_helpers.py, _SCALAR_VALUE_TO_JSON_PARAM: the insertAll spellings, except
TIMESTAMP as "YYYY-MM-DD HH:MM:SS.ffffff+00:00"), parsed by stream.py's rules; anything else is
refused. The values are bound as DuckDB parameters (`$p_<name>`), never written into the SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from crucible.server.bigquery import stream

# Standard type names, as the client sends them, to the legacy names stream.py parses.
_SCALARS = {
    "INT64": "INTEGER",
    "FLOAT64": "FLOAT",
    "NUMERIC": "NUMERIC",
    "BOOL": "BOOLEAN",
    "STRING": "STRING",
    "BYTES": "BYTES",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMP",
    "JSON": "JSON",
}
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?\+00:00"
)  # _timestamp_to_json_parameter


@dataclass(frozen=True)
class Parameters:
    types: dict[str, str]  # parameter name (or 1-based position) -> BigQuery type SQL
    values: dict[str, Any]  # DuckDB parameter name (p_<name>) -> Python value


def bind_name(name: str) -> str:
    return f"p_{name.lower()}"


def parse(config: dict[str, Any]) -> Parameters:
    raw = config.get("queryParameters")
    if not raw:
        return Parameters({}, {})
    items = cast("list[dict[str, Any]]", raw)
    mode = str(config.get("parameterMode") or ("POSITIONAL" if "name" not in items[0] else "NAMED"))
    if mode.upper() not in ("NAMED", "POSITIONAL"):
        raise CrucibleError(ErrorKind.INVALID_QUERY, f"Invalid parameterMode: {mode}")
    named = mode.upper() == "NAMED"
    types: dict[str, str] = {}
    values: dict[str, Any] = {}
    for position, item in enumerate(items, start=1):
        if named and not item.get("name"):
            raise CrucibleError(ErrorKind.INVALID_QUERY, "Named query parameters need a name")
        name = str(item["name"]).lower() if named else str(position)
        kind = cast("dict[str, Any]", item.get("parameterType") or {})
        types[name] = type_sql(kind)
        values[bind_name(name)] = _value(kind, item.get("parameterValue"), name)
    return Parameters(types, values)


def type_sql(kind: dict[str, Any]) -> str:
    """The BigQuery type of a parameterType, as SQL (`ARRAY<STRUCT<a INT64>>`)."""
    name = str(kind.get("type", "")).upper()
    if name == "ARRAY":
        return f"ARRAY<{type_sql(cast('dict[str, Any]', kind.get('arrayType') or {}))}>"
    if name == "STRUCT":
        fields = cast("list[dict[str, Any]]", kind.get("structTypes") or [])
        if any(not f.get("name") for f in fields):
            raise CrucibleUnsupportedError("a query parameter STRUCT field without a name")
        inner = ", ".join(f"`{f['name']}` {type_sql(f.get('type') or {})}" for f in fields)
        return f"STRUCT<{inner}>"
    if name not in _SCALARS:
        raise CrucibleUnsupportedError(f"query parameter type {name or '(none)'}")
    return name


def _value(kind: dict[str, Any], value: Any, location: str) -> Any:
    name = str(kind.get("type", "")).upper()
    holder = cast("dict[str, Any]", value or {})
    if name == "ARRAY":
        element = cast("dict[str, Any]", kind.get("arrayType") or {})
        items = cast("list[Any]", holder.get("arrayValues") or [])
        return [_value(element, item, location) for item in items]
    if name == "STRUCT":
        fields = cast("list[dict[str, Any]]", kind.get("structTypes") or [])
        given = cast("dict[str, Any]", holder.get("structValues") or {})
        unknown = set(given) - {f["name"] for f in fields}
        if unknown:
            raise CrucibleUnsupportedError(f"query parameter STRUCT value {sorted(unknown)[0]}")
        if not given and "structValues" not in holder:
            return None
        return {
            f["name"]: _value(f.get("type") or {}, given.get(f["name"]), location) for f in fields
        }
    scalar = holder.get("value")
    if scalar is None:
        return None
    if name == "TIMESTAMP" and isinstance(scalar, str) and _TIMESTAMP.fullmatch(scalar):
        return datetime.fromisoformat(scalar).astimezone(UTC)
    try:
        return stream.scalar(_SCALARS[name], scalar, location)
    except stream.RowError as exc:
        raise CrucibleUnsupportedError(f"query parameter {location} value {scalar!r}") from exc
