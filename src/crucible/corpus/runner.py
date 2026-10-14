"""Run corpus cases against Crucible and compare with what each case expects (ADR 0013)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from crucible.corpus.case import Case, Expected
from crucible.engine import CrucibleError, Engine, ErrorKind, Pipeline, Result

if TYPE_CHECKING:
    from crucible.dialects.base import Dialect

# Namespace every case runs in (ADR 0013, "Names").
CORPUS_CATALOG = "crucible-corpus"
CORPUS_SCHEMA = "corpus"

# An error of these kinds never passes an expected-error case (ADR 0013, "Errors").
NOT_PASSING_KINDS = frozenset({ErrorKind.INTERNAL.value, ErrorKind.UNSUPPORTED.value})
MAX_DETAIL_ROWS = 5


@dataclass
class Outcome:
    case: Case
    passed: bool
    detail: str  # explains a mismatch readably; a short note when the case passes


class SetupError(Exception):
    """A setup statement failed in Crucible. The case cannot be judged, so it fails."""


def actual_for(case: Case, dialect: Dialect) -> Expected:
    """Run the case in a fresh engine and describe what Crucible answered."""
    engine = Engine()
    try:
        session = dialect.new_session(CORPUS_CATALOG, CORPUS_SCHEMA)
        with engine.cursor(session) as cursor:
            engine.catalog(cursor).ensure_schema(CORPUS_CATALOG, CORPUS_SCHEMA)
        pipeline = Pipeline(engine, dialect)
        for statement in case.setup:
            try:
                pipeline.execute(session, statement)
            except CrucibleError as exc:
                raise SetupError(f"{statement!r}: {exc}") from exc
        try:
            return _result_expected(pipeline.execute(session, case.query), dialect)
        except CrucibleError as exc:
            return Expected(error_message=str(exc), error_kind=exc.kind.value)
        except Exception as exc:  # noqa: BLE001 - a Crucible bug: report it on this case, keep going
            message = f"{type(exc).__name__}: {exc}"
            return Expected(error_message=message, error_kind=ErrorKind.INTERNAL.value)
    finally:
        engine.close()


def compare(case: Case, actual: Expected) -> Outcome:
    if case.expected.columns is None:
        return _compare_error(case, actual)
    return _compare_rows(case, actual)


def run_cases(cases: list[Case], dialect: Dialect) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for case in cases:
        try:
            outcomes.append(compare(case, actual_for(case, dialect)))
        except SetupError as exc:
            outcomes.append(Outcome(case, False, f"setup failed: {exc}"))
    return outcomes


def _result_expected(results: list[Result], dialect: Dialect) -> Expected:
    """Columns and rows of the query's last result, rendered by the dialect."""
    if not results:
        return Expected(columns=[], rows=[])
    last = results[-1]
    columns = [(c.name, dialect.warehouse_type(c.type)) for c in last.columns]
    types = [kind for _, kind in columns]
    rows = [
        [dialect.canonical_value(value, kind) for value, kind in zip(row, types, strict=True)]
        for row in last.rows
    ]
    return Expected(columns=columns, rows=rows)


def _compare_error(case: Case, actual: Expected) -> Outcome:
    if actual.error_kind is None:
        return Outcome(case, False, f"expected an error, got {len(actual.rows or [])} row(s)")
    message = actual.error_message or ""
    if actual.error_kind in NOT_PASSING_KINDS:
        return Outcome(
            case,
            False,
            f"Crucible raised a {actual.error_kind} error, which does not pass: {message}",
        )
    if actual.error_kind != case.expected.error_kind:
        return Outcome(
            case,
            False,
            f"expected a {case.expected.error_kind} error, Crucible raised {actual.error_kind}: "
            f"{message}",
        )
    contains = case.expected.error_contains
    if contains is not None and contains not in message:
        return Outcome(case, False, f"error {message!r} does not contain {contains!r}")
    return Outcome(case, True, f"{actual.error_kind} error: {message}")


def _compare_rows(case: Case, actual: Expected) -> Outcome:
    if actual.error_kind is not None:
        return Outcome(case, False, f"unexpected {actual.error_kind} error: {actual.error_message}")
    expected_columns = case.expected.columns or []
    if actual.columns != expected_columns:
        return Outcome(
            case, False, f"columns differ: expected {expected_columns}, got {actual.columns}"
        )
    expected_rows = case.expected.rows or []
    actual_rows = actual.rows or []
    if case.ordered:
        mismatch = _first_difference(expected_rows, actual_rows)
        if mismatch is not None:
            return Outcome(case, False, mismatch)
        return Outcome(case, True, f"{len(actual_rows)} row(s) match, in order")
    missing, extra = _multiset_difference(expected_rows, actual_rows)
    if missing or extra:
        return Outcome(
            case,
            False,
            f"rows differ: missing {missing or '[]'}, unexpected {extra or '[]'}",
        )
    return Outcome(case, True, f"{len(actual_rows)} row(s) match")


def _first_difference(expected: list[list[object]], actual: list[list[object]]) -> str | None:
    for index in range(max(len(expected), len(actual))):
        want = expected[index] if index < len(expected) else "<no row>"
        got = actual[index] if index < len(actual) else "<no row>"
        if want != got:
            return f"row {index} differs: expected {want!r}, got {got!r}"
    return None


def _multiset_difference(
    expected: list[list[object]], actual: list[list[object]]
) -> tuple[list[str], list[str]]:
    """Rows expected but not produced, and rows produced but not expected (ignoring order)."""
    want = Counter(_row_key(row) for row in expected)
    got = Counter(_row_key(row) for row in actual)
    missing = sorted((want - got).elements())[:MAX_DETAIL_ROWS]
    extra = sorted((got - want).elements())[:MAX_DETAIL_ROWS]
    return missing, extra


def _row_key(row: list[object]) -> str:
    return json.dumps(row, sort_keys=True, default=str)
