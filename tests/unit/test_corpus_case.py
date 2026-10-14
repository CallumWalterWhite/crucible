"""Case file loading, validation and recording writes (ADR 0013)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from crucible.corpus.case import Expected, find_cases, load_case, write_recorded

VALID = """\
description: double a value
tags: [math]
setup:
  - CREATE TABLE t (a INTEGER)
  - INSERT INTO t VALUES (1), (2)
query: SELECT a * 2 AS d FROM t ORDER BY a
ordered: true
expected:
  columns:
    - {name: d, type: INT64}
  rows:
    - [2]
    - [4]
verified: false
recorded_at: null
"""


def write_case(
    root: Path, text: str, *, dialect: str = "passthrough", area: str = "math", name: str = "double"
) -> Path:
    path = root / "corpus" / dialect / area / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def with_change(text: str, mutate: Any) -> str:
    doc = yaml.safe_load(text)
    mutate(doc)
    return yaml.safe_dump(doc, sort_keys=False)


def with_expected(block: str) -> str:
    """VALID with its `expected:` block replaced by `block` (already indented)."""
    head = VALID.split("expected:\n")[0]
    return f"{head}expected:\n{block}verified: false\nrecorded_at: null\n"


def test_loads_a_valid_case(tmp_path: Path):
    case = load_case(write_case(tmp_path, VALID))
    assert (case.dialect, case.area, case.name) == ("passthrough", "math", "double")
    assert case.description == "double a value"
    assert case.tags == ["math"]
    assert case.setup == ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (2)"]
    assert case.ordered is True
    assert case.expected.columns == [("d", "INT64")]
    assert case.expected.rows == [[2], [4]]
    assert case.verified is False
    assert case.recorded_at is None


def test_loads_an_error_case_with_its_kind(tmp_path: Path):
    text = with_expected('  error: {kind: runtime, message: "boom", message_contains: "boo"}\n')
    case = load_case(write_case(tmp_path, text))
    assert case.expected == Expected(
        error_message="boom", error_contains="boo", error_kind="runtime"
    )


@pytest.mark.parametrize(
    ("error_block", "problem"),
    [
        ("  error: {}\n", "expected.error.kind must be one of"),
        ('  error: {message_contains: "boo"}\n', "expected.error.kind must be one of"),
        (
            '  error: {kind: internal, message_contains: "boo"}\n',
            "expected.error.kind must be one of",
        ),
        (
            "  error: {kind: runtime}\n",
            "expected.error.message_contains must be a non-empty string",
        ),
        (
            '  error: {kind: runtime, message_contains: ""}\n',
            "expected.error.message_contains must be a non-empty string",
        ),
    ],
)
def test_rejects_error_expectations_without_a_kind_or_a_substring(
    tmp_path: Path, error_block: str, problem: str
):
    path = write_case(tmp_path, with_expected(error_block))
    with pytest.raises(ValueError, match=re.escape(problem)) as info:
        load_case(path)
    assert str(path) in str(info.value)  # the error names the file


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda d: d.update(colour="red"), "unknown top-level keys: colour"),
        (lambda d: d.pop("query"), "missing required keys: query"),
        (
            lambda d: d.update(expected={"error": {"message": "x"}, "rows": []}),
            "expected has an error and also columns or rows",
        ),
        (lambda d: d.update(verified=True), "verified: true needs a recorded_at"),
        (lambda d: d.update(expected={"columns": [], "rows": [[1]]}), "expected.rows[0]"),
    ],
)
def test_rejects_invalid_cases(tmp_path: Path, mutate: Any, problem: str):
    path = write_case(tmp_path, with_change(VALID, mutate))
    with pytest.raises(ValueError, match=re.escape(problem)) as info:
        load_case(path)
    assert str(path) in str(info.value)  # the error names the file


def test_rejects_a_path_outside_corpus_dialect_area(tmp_path: Path):
    flat = tmp_path / "corpus" / "passthrough" / "double.yaml"
    flat.parent.mkdir(parents=True)
    flat.write_text(VALID, encoding="utf-8")
    with pytest.raises(
        ValueError, match=re.escape("must live at corpus/<dialect>/<area>/<name>.yaml")
    ):
        load_case(flat)

    elsewhere = tmp_path / "cases" / "passthrough" / "math" / "double.yaml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(VALID, encoding="utf-8")
    with pytest.raises(
        ValueError, match=re.escape("must live at corpus/<dialect>/<area>/<name>.yaml")
    ):
        load_case(elsewhere)


def test_find_cases_is_sorted_by_path(tmp_path: Path):
    for name in ("b", "a", "c"):
        write_case(tmp_path, VALID, name=name)
    write_case(tmp_path, VALID, area="text", name="a")
    cases = find_cases(tmp_path / "corpus")
    assert [(c.area, c.name) for c in cases] == [
        ("math", "a"),
        ("math", "b"),
        ("math", "c"),
        ("text", "a"),
    ]


def test_write_recorded_changes_only_three_fields(tmp_path: Path):
    path = write_case(tmp_path, VALID)
    case = load_case(path)
    before = yaml.safe_load(path.read_text(encoding="utf-8"))
    new = Expected(columns=[("d", "INT64")], rows=[[4], [2]])

    write_recorded(case, new, "2026-10-14T12:00:00Z")

    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert list(after) == list(before)  # same keys in the same order
    assert after["expected"] == {"columns": [{"name": "d", "type": "INT64"}], "rows": [[4], [2]]}
    assert after["verified"] is True
    assert after["recorded_at"] == "2026-10-14T12:00:00Z"
    for key in before:
        if key not in ("expected", "verified", "recorded_at"):
            assert after[key] == before[key], key


def test_write_recorded_writes_an_error_with_its_kind_and_narrowing(tmp_path: Path):
    path = write_case(tmp_path, VALID)
    case = load_case(path)
    recorded = Expected(
        error_message="division by zero", error_contains="zero", error_kind="runtime"
    )

    write_recorded(case, recorded, "2026-10-14T12:00:00Z")

    reloaded = load_case(path)
    assert reloaded.expected == recorded
    assert reloaded.verified is True


def test_write_recorded_can_leave_a_case_unverified(tmp_path: Path):
    path = write_case(tmp_path, VALID)
    case = load_case(path)
    recorded = Expected(error_message="boom", error_contains="nope", error_kind="runtime")

    write_recorded(case, recorded, "2026-10-14T12:00:00Z", verified=False)

    reloaded = load_case(path)
    assert reloaded.expected == recorded
    assert reloaded.verified is False
    assert reloaded.recorded_at == "2026-10-14T12:00:00Z"
