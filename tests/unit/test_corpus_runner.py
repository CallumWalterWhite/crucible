"""Runner comparison rules (ADR 0013), with the test-only PassthroughDialect."""

from __future__ import annotations

from pathlib import Path

from crucible.corpus.case import Case, Expected, find_cases, load_case
from crucible.corpus.runner import actual_for, compare, run_cases
from tests.fakes import PassthroughDialect

DIALECT = PassthroughDialect()


def case_file(root: Path, name: str, body: str) -> Case:
    path = root / "corpus" / "passthrough" / "runner" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"description: {name}\n{body}", encoding="utf-8")
    return load_case(path)


def setup_yaml(statements: list[str]) -> str:
    return (
        "setup:\n" + "".join(f"  - {statement}\n" for statement in statements) if statements else ""
    )


def rows_case(
    root: Path,
    name: str,
    query: str,
    columns: str,
    rows: str,
    *,
    ordered: bool = False,
    setup: list[str] | None = None,
) -> Case:
    body = (
        f"{setup_yaml(setup or [])}query: {query}\nordered: {str(ordered).lower()}\n"
        f"expected:\n  columns:\n{columns}  rows:\n{rows}"
    )
    return case_file(root, name, body)


def error_case(root: Path, name: str, query: str, kind: str, message_contains: str) -> Case:
    error = f'{{kind: {kind}, message_contains: "{message_contains}"}}'
    return case_file(root, name, f"query: {query}\nexpected:\n  error: {error}\n")


def test_passing_case(tmp_path: Path):
    case = rows_case(
        tmp_path,
        "passing",
        "SELECT 1::BIGINT AS a, 'x' AS b",
        "    - {name: a, type: BIGINT}\n    - {name: b, type: TEXT}\n",
        "    - [1, x]\n",
    )
    [outcome] = run_cases([case], DIALECT)
    assert outcome.passed, outcome.detail


def test_row_mismatch_fails_with_detail(tmp_path: Path):
    case = rows_case(
        tmp_path,
        "mismatch",
        "SELECT 1::BIGINT AS a",
        "    - {name: a, type: BIGINT}\n",
        "    - [2]\n",
    )
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert "missing" in outcome.detail
    assert "[2]" in outcome.detail
    assert "unexpected" in outcome.detail
    assert "[1]" in outcome.detail


def test_unordered_rows_are_a_multiset(tmp_path: Path):
    setup = ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (2)"]
    query = "SELECT a FROM t ORDER BY a DESC"
    columns = "    - {name: a, type: INT}\n"
    reversed_rows = "    - [1]\n    - [2]\n"
    assert run_cases(
        [rows_case(tmp_path, "unordered", query, columns, reversed_rows, setup=setup)], DIALECT
    )[0].passed

    ordered = rows_case(
        tmp_path, "ordered-wrong", query, columns, reversed_rows, ordered=True, setup=setup
    )
    [outcome] = run_cases([ordered], DIALECT)
    assert not outcome.passed
    assert "row 0 differs" in outcome.detail


def test_ordered_rows_pass_in_order(tmp_path: Path):
    setup = ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (2)"]
    case = rows_case(
        tmp_path,
        "ordered-right",
        "SELECT a FROM t ORDER BY a DESC",
        "    - {name: a, type: INT}\n",
        "    - [2]\n    - [1]\n",
        ordered=True,
        setup=setup,
    )
    [outcome] = run_cases([case], DIALECT)
    assert outcome.passed, outcome.detail


def test_column_type_mismatch_fails(tmp_path: Path):
    case = rows_case(
        tmp_path, "type", "SELECT 1::BIGINT AS a", "    - {name: a, type: INT}\n", "    - [1]\n"
    )
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert "columns differ" in outcome.detail


def test_expected_error_passes_when_message_contains_matches(tmp_path: Path):
    case = error_case(
        tmp_path, "error", "SELECT CAST('x' AS INTEGER) AS a", "runtime", "Could not convert"
    )
    [outcome] = run_cases([case], DIALECT)
    assert outcome.passed, outcome.detail
    assert "runtime error" in outcome.detail


def test_expected_error_fails_when_message_does_not_match(tmp_path: Path):
    case = error_case(
        tmp_path, "wrong-text", "SELECT CAST('x' AS INTEGER) AS a", "runtime", "no such thing"
    )
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert "does not contain" in outcome.detail


def test_unexpected_error_fails(tmp_path: Path):
    case = rows_case(
        tmp_path,
        "unexpected",
        "SELECT CAST('x' AS INTEGER) AS a",
        "    - {name: a, type: INT}\n",
        "    - [1]\n",
    )
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert "unexpected runtime error" in outcome.detail


def test_internal_and_unsupported_errors_never_pass(tmp_path: Path):
    case = error_case(tmp_path, "any-error", "SELECT 1", "runtime", "x")
    for kind in ("internal", "unsupported"):
        outcome = compare(case, Expected(error_message="x", error_kind=kind))
        assert not outcome.passed, kind
        assert "does not pass" in outcome.detail


def test_expected_error_fails_when_kind_differs_even_if_message_matches(tmp_path: Path):
    # A syntax error is invalid_query; the case says runtime. Message matches, kind does not.
    case = error_case(tmp_path, "wrong-kind", "SELECT FROM", "runtime", "syntax error")
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert "expected a runtime error" in outcome.detail
    assert "Crucible raised invalid_query" in outcome.detail


def test_expected_error_fails_when_no_error_is_raised(tmp_path: Path):
    case = error_case(tmp_path, "no-error", "SELECT 1 AS a", "runtime", "x")
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert "expected an error" in outcome.detail


def test_setup_failure_fails_the_case(tmp_path: Path):
    case = rows_case(
        tmp_path,
        "bad-setup",
        "SELECT 1::BIGINT AS a",
        "    - {name: a, type: BIGINT}\n",
        "    - [1]\n",
        setup=["SELECT * FROM no_such_table"],
    )
    [outcome] = run_cases([case], DIALECT)
    assert not outcome.passed
    assert outcome.detail.startswith("setup failed:")


def test_each_case_runs_in_a_fresh_namespace(tmp_path: Path):
    first = rows_case(
        tmp_path,
        "a-creates",
        "SELECT a FROM t",
        "    - {name: a, type: INT}\n",
        "    - [1]\n",
        setup=["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1)"],
    )
    second = error_case(tmp_path, "b-no-table", "SELECT a FROM t", "not_found", "does not exist")
    outcomes = run_cases([first, second], DIALECT)
    assert [o.passed for o in outcomes] == [True, True], [o.detail for o in outcomes]


def test_actual_for_describes_the_result(tmp_path: Path):
    case = rows_case(
        tmp_path,
        "shape",
        "SELECT 1::BIGINT AS a, 'x' AS b",
        "    - {name: a, type: BIGINT}\n",
        "    - [1]\n",
    )
    actual = actual_for(case, DIALECT)
    assert actual.columns == [("a", "BIGINT"), ("b", "TEXT")]
    assert actual.rows == [[1, "x"]]
    assert actual.error_kind is None


def test_find_cases_on_a_directory_runs_each_file(tmp_path: Path):
    rows_case(
        tmp_path, "one", "SELECT 1::BIGINT AS a", "    - {name: a, type: BIGINT}\n", "    - [1]\n"
    )
    rows_case(
        tmp_path, "two", "SELECT 2::BIGINT AS a", "    - {name: a, type: BIGINT}\n", "    - [1]\n"
    )
    outcomes = run_cases(find_cases(tmp_path / "corpus" / "passthrough"), DIALECT)
    assert [o.case.name for o in outcomes] == ["one", "two"]
    assert [o.passed for o in outcomes] == [True, False]
