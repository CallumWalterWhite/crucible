"""Every corpus case, run against Crucible. Unverified cases run too (ADR 0013, "Running")."""

from __future__ import annotations

from pathlib import Path

import pytest

from crucible.corpus.case import Case, find_cases
from crucible.corpus.runner import run_cases
from crucible.dialects import get_dialect
from crucible.engine import CrucibleUnsupportedError

CORPUS_ROOT = Path(__file__).resolve().parents[2] / "corpus"
CASES = find_cases(CORPUS_ROOT)
KNOWN_DIVERGENCE = "known-divergence"


def _param(case: Case):
    # A known divergence must fail; strict means a fix that makes it pass fails the run until the
    # tag is removed, so tags always match what Crucible does.
    marks = (
        [pytest.mark.xfail(strict=True, reason="tagged known-divergence")]
        if KNOWN_DIVERGENCE in case.tags
        else []
    )
    return pytest.param(case, id=f"{case.dialect}/{case.area}/{case.name}", marks=marks)


# With no cases yet, one placeholder keeps the module green instead of skipping everything.
PARAMS = [_param(case) for case in CASES] or [pytest.param(None, id="no-cases")]


@pytest.mark.parametrize("case", PARAMS)
def test_case(case: Case | None):
    if case is None:
        return
    try:
        dialect = get_dialect(case.dialect)
    except CrucibleUnsupportedError as exc:
        pytest.skip(str(exc))
    [outcome] = run_cases([case], dialect)
    assert outcome.passed, outcome.detail
