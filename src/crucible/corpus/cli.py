"""`crucible corpus run|record`. The argparse definitions live in crucible/cli.py."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from crucible.corpus.case import Case, find_cases
from crucible.corpus.runner import Outcome, run_cases
from crucible.dialects import get_dialect
from crucible.dialects.base import Dialect
from crucible.engine import CrucibleUnsupportedError

# Crucible bills nothing; the recorder path sends a cap on every job, so any value works.
WIRE_MAX_BYTES_BILLED = 1


def main(args: argparse.Namespace) -> int:
    if args.corpus_command == "run":
        return _run(args)
    if args.corpus_command == "record":
        return _record(args)
    if args.corpus_command == "record-api":
        return _record_api(args)
    print(f"unknown corpus command {args.corpus_command!r}", file=sys.stderr)
    return 2


def _run(args: argparse.Namespace) -> int:
    try:
        dialect = get_dialect(args.dialect)
        cases = find_cases(Path(args.path or f"corpus/{args.dialect}"))
    except (CrucibleUnsupportedError, ValueError) as exc:
        print(f"crucible corpus run: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "wire", None):
        outcomes = _wire_outcomes(args, cases, dialect)
        if outcomes is None:
            return 2
    else:
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


def _wire_outcomes(
    args: argparse.Namespace, cases: list[Case], dialect: Dialect
) -> list[Outcome] | None:
    """Every case through google-cloud-bigquery against a running Crucible server."""
    if args.dialect != "bigquery":
        print("--wire is available for bigquery only", file=sys.stderr)
        return None
    try:
        from google.api_core.client_options import ClientOptions
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import bigquery

        from crucible.corpus.record import BigQueryRecorder, wire_outcomes
    except ImportError as exc:
        extra = "pip install 'crucible-emulator[record-bigquery]'"
        print(f"crucible corpus run --wire needs: {extra} ({exc})", file=sys.stderr)
        return None
    client = bigquery.Client(
        project="crucible-wire",
        credentials=AnonymousCredentials(),  # pyright: ignore[reportArgumentType]
        client_options=ClientOptions(api_endpoint=args.wire),
    )
    return wire_outcomes(cases, BigQueryRecorder(client, dialect, WIRE_MAX_BYTES_BILLED))


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
    if getattr(args, "unverified_only", False):
        cases = [c for c in cases if not c.verified]
        if not cases:
            print("no unverified cases to record")
            return 0
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


def _record_api(args: argparse.Namespace) -> int:
    if not args.confirmed:
        print(
            "refusing to record: record-api runs jobs against the real warehouse and spends "
            "credits. Pass --i-understand-this-spends-credits to proceed.",
            file=sys.stderr,
        )
        return 2
    project = os.environ.get("BIGQUERY_PROJECT")
    if not project:
        print("BIGQUERY_PROJECT must be set to the sandbox project", file=sys.stderr)
        return 2
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        from crucible.corpus.record_api import RecordApiError, record_api
    except ImportError as exc:
        extra = "pip install 'crucible-emulator[record-bigquery]'"
        print(f"crucible corpus record-api needs: {extra} ({exc})", file=sys.stderr)
        return 2
    out = Path(args.out)
    print(
        f"Recording BigQuery API fixtures against project {project} into {out} with "
        f"maximum_bytes_billed={args.max_bytes_billed} per query job"
    )
    # google.auth is untyped: Application Default Credentials (gcloud, or Workload Identity in CI).
    credentials = cast(
        object,
        google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery"])[0],  # pyright: ignore[reportUnknownMemberType]
    )
    session = AuthorizedSession(credentials)  # pyright: ignore[reportArgumentType]
    try:
        record_api(session, project, args.max_bytes_billed, out, print)
    except RecordApiError as exc:
        print(f"crucible corpus record-api stopped: {exc}", file=sys.stderr)
        return 1
    return 0
