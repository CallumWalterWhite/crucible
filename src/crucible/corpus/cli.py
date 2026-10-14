"""`crucible corpus run|record`. The argparse definitions live in crucible/cli.py."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from crucible.corpus.case import find_cases
from crucible.corpus.runner import run_cases
from crucible.dialects import get_dialect
from crucible.engine import CrucibleUnsupportedError


def main(args: argparse.Namespace) -> int:
    if args.corpus_command == "run":
        return _run(args)
    if args.corpus_command == "record":
        return _record(args)
    print(f"unknown corpus command {args.corpus_command!r}", file=sys.stderr)
    return 2


def _run(args: argparse.Namespace) -> int:
    try:
        dialect = get_dialect(args.dialect)
        cases = find_cases(Path(args.path or f"corpus/{args.dialect}"))
    except (CrucibleUnsupportedError, ValueError) as exc:
        print(f"crucible corpus run: {exc}", file=sys.stderr)
        return 2
    outcomes = run_cases(cases, dialect)
    for outcome in outcomes:
        if not outcome.passed:
            print(f"FAIL {outcome.case.path}: {outcome.detail}")
    passed = sum(outcome.passed for outcome in outcomes)
    failed = len(outcomes) - passed
    verified = sum(case.verified for case in cases)
    unverified = len(cases) - verified
    print(f"{passed} passed, {failed} failed ({verified} verified, {unverified} unverified)")
    return 1 if failed else 0


def _record(args: argparse.Namespace) -> int:
    if not args.confirmed:
        print(
            "refusing to record: it runs every case against the real warehouse and spends "
            "credits. Pass --i-understand-this-spends-credits to proceed.",
            file=sys.stderr,
        )
        return 2
    if args.dialect != "bigquery":
        print(
            "snowflake recording is not available until the Snowflake dialect exists",
            file=sys.stderr,
        )
        return 2
    project = os.environ.get("BIGQUERY_PROJECT")
    if not project:
        print("BIGQUERY_PROJECT must be set to the project that runs the cases", file=sys.stderr)
        return 2
    try:
        from google.cloud import bigquery

        from crucible.corpus.record import BigQueryRecorder, RecordError, record_cases
    except ImportError as exc:
        extra = "pip install 'crucible-emulator[record-bigquery]'"
        print(f"crucible corpus record needs: {extra} ({exc})", file=sys.stderr)
        return 2
    try:
        dialect = get_dialect(args.dialect)
        cases = find_cases(Path(args.path or f"corpus/{args.dialect}"))
    except (CrucibleUnsupportedError, ValueError) as exc:
        print(f"crucible corpus record: {exc}", file=sys.stderr)
        return 2
    jobs = sum(len(c.setup) + 1 for c in cases)
    print(
        f"Recording {len(cases)} cases ({jobs} jobs) against project {project} "
        f"with maximum_bytes_billed={args.max_bytes_billed} per job; "
        f"worst case {jobs * args.max_bytes_billed} bytes billed in total"
    )
    client = bigquery.Client(project=project)
    recorder = BigQueryRecorder(client, dialect, args.max_bytes_billed)
    try:
        mismatched = record_cases(cases, recorder, now=lambda: datetime.now(UTC))
    except RecordError as exc:
        print(f"crucible corpus record stopped: {exc}", file=sys.stderr)
        return 1
    for case in mismatched:
        print(f"needs a new message_contains (left unverified): {case.path}")
    print(f"recorded {len(cases)} cases, {len(cases) - len(mismatched)} verified")
    return 0
