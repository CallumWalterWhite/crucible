"""Corpus case files: one YAML file per case (format in docs/adr/0013)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

TOP_LEVEL_KEYS = frozenset(
    {"description", "tags", "setup", "query", "ordered", "expected", "verified", "recorded_at"}
)
REQUIRED_KEYS = frozenset({"description", "query", "expected"})
ERROR_KEYS = frozenset({"kind", "message", "message_contains"})
# Crucible ErrorKind values a case may expect; BigQuery never answers "internal" or "unsupported".
EXPECTED_ERROR_KINDS = frozenset(
    {"invalid_query", "not_found", "already_exists", "conflict", "runtime"}
)


@dataclass
class Expected:
    """What a case expects (from its file) or what an actual run produced (from the runner)."""

    columns: list[tuple[str, str]] | None = None  # (name, warehouse type)
    rows: list[list[Any]] | None = None
    error_message: str | None = None  # the warehouse's message, when recorded
    error_contains: str | None = None  # substring Crucible's message must contain
    error_kind: str | None = None  # ErrorKind value: expected by the case, or raised by Crucible


@dataclass
class Case:
    path: Path
    dialect: str
    area: str
    name: str
    description: str
    tags: list[str]
    setup: list[str]
    query: str
    ordered: bool
    expected: Expected
    verified: bool
    recorded_at: str | None


def load_case(path: Path) -> Case:
    """Read and validate one case file. Raises ValueError naming the file and the problem."""
    dialect, area, name = _location(path)
    try:
        doc: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot read YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: the top level must be a mapping")
    fields = cast(dict[str, Any], doc)

    unknown = sorted(str(key) for key in fields if key not in TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown top-level keys: {', '.join(unknown)}")
    missing = sorted(REQUIRED_KEYS - fields.keys())
    if missing:
        raise ValueError(f"{path}: missing required keys: {', '.join(missing)}")

    description = _text(path, fields, "description")
    query = _text(path, fields, "query")
    recorded_at = fields.get("recorded_at")
    if recorded_at is not None and not isinstance(recorded_at, str):
        raise ValueError(f"{path}: recorded_at must be an ISO-8601 string or null")
    verified = fields.get("verified", False)
    if not isinstance(verified, bool):
        raise ValueError(f"{path}: verified must be true or false")
    if verified and recorded_at is None:
        raise ValueError(f"{path}: verified: true needs a recorded_at timestamp")
    ordered = fields.get("ordered", False)
    if not isinstance(ordered, bool):
        raise ValueError(f"{path}: ordered must be true or false")

    return Case(
        path=path,
        dialect=dialect,
        area=area,
        name=name,
        description=description,
        tags=_str_list(path, fields, "tags"),
        setup=_str_list(path, fields, "setup"),
        query=query,
        ordered=ordered,
        expected=_expected(path, fields["expected"]),
        verified=verified,
        recorded_at=recorded_at,
    )


def find_cases(root: Path) -> list[Case]:
    """Every case under `root` (a directory, or one case file), sorted by path."""
    if root.is_file():
        return [load_case(root)]
    if not root.is_dir():
        raise ValueError(f"{root}: no such case file or directory")
    return [load_case(path) for path in sorted(root.rglob("*.yaml"))]


def write_recorded(
    case: Case, expected: Expected, recorded_at: str, *, verified: bool = True
) -> None:
    """Rewrite only `expected`, `verified` and `recorded_at`; other keys keep their order.

    YAML comments are not preserved (ADR 0013).
    """
    doc = cast(dict[str, Any], yaml.safe_load(case.path.read_text(encoding="utf-8")))
    doc["expected"] = _expected_doc(expected)
    doc["verified"] = verified
    doc["recorded_at"] = recorded_at
    case.path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _location(path: Path) -> tuple[str, str, str]:
    """(dialect, area, name) from corpus/<dialect>/<area>/<name>.yaml."""
    if path.suffix != ".yaml" or path.parent.parent.parent.name != "corpus":
        raise ValueError(f"{path}: a case must live at corpus/<dialect>/<area>/<name>.yaml")
    return path.parent.parent.name, path.parent.name, path.stem


def _expected(path: Path, raw: object) -> Expected:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected must be a mapping")
    keys = set(cast(dict[str, Any], raw))
    if "error" in keys:
        if keys != {"error"}:
            raise ValueError(f"{path}: expected has an error and also columns or rows")
        error = cast(object, raw["error"])
        if not isinstance(error, dict):
            raise ValueError(f"{path}: expected.error must be a mapping")
        error_fields = cast(dict[str, Any], error)
        unknown = sorted(str(key) for key in error_fields if key not in ERROR_KEYS)
        if unknown:
            raise ValueError(f"{path}: expected.error takes kind, message, message_contains only")
        # ADR 0013: an expected error must say which error, so a binder error cannot pass for
        # a runtime check.
        kind = error_fields.get("kind")
        if kind not in EXPECTED_ERROR_KINDS:
            allowed = ", ".join(sorted(EXPECTED_ERROR_KINDS))
            raise ValueError(f"{path}: expected.error.kind must be one of: {allowed}")
        message = error_fields.get("message")
        contains = error_fields.get("message_contains")
        if not isinstance(contains, str) or not contains:
            raise ValueError(f"{path}: expected.error.message_contains must be a non-empty string")
        if message is not None and not isinstance(message, str):
            raise ValueError(f"{path}: expected.error.message must be a string")
        return Expected(error_message=message, error_contains=contains, error_kind=str(kind))

    if keys != {"columns", "rows"}:
        raise ValueError(f"{path}: expected needs columns and rows, or an error")
    shape = cast(dict[str, Any], raw)
    columns = _columns(path, shape["columns"])
    rows = shape["rows"]
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected.rows must be a list")
    for index, row in enumerate(cast(list[object], rows)):
        if not isinstance(row, list) or len(cast(list[object], row)) != len(columns):
            raise ValueError(f"{path}: expected.rows[{index}] must have {len(columns)} values")
    return Expected(columns=columns, rows=cast(list[list[Any]], rows))


def _columns(path: Path, raw: object) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected.columns must be a list")
    columns: list[tuple[str, str]] = []
    for index, item in enumerate(cast(list[object], raw)):
        if not isinstance(item, dict) or set(cast(dict[str, Any], item)) != {"name", "type"}:
            raise ValueError(f"{path}: expected.columns[{index}] needs exactly name and type")
        column = cast(dict[str, Any], item)
        if not isinstance(column["name"], str) or not isinstance(column["type"], str):
            raise ValueError(f"{path}: expected.columns[{index}] name and type must be strings")
        columns.append((column["name"], column["type"]))
    return columns


def _expected_doc(expected: Expected) -> dict[str, Any]:
    if expected.columns is not None:
        return {
            "columns": [{"name": name, "type": kind} for name, kind in expected.columns],
            "rows": expected.rows or [],
        }
    error: dict[str, Any] = {}
    if expected.error_kind is not None:
        error["kind"] = expected.error_kind
    if expected.error_message is not None:
        error["message"] = expected.error_message
    if expected.error_contains is not None:
        error["message_contains"] = expected.error_contains
    return {"error": error}


def _text(path: Path, fields: dict[str, Any], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {key} must be a non-empty string")
    return value


def _str_list(path: Path, fields: dict[str, Any], key: str) -> list[str]:
    value: object = fields.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{path}: {key} must be a list of strings")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{path}: {key} must be a list of strings")
    return [str(item) for item in items]
