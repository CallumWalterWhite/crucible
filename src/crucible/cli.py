"""`crucible serve`, `crucible corpus run|record`, `crucible mirror-schema`."""

from __future__ import annotations

import argparse
import os
import sys

DIALECTS = ("bigquery", "snowflake")


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from crucible.engine import Engine
        from crucible.server.app import create_app
        from crucible.server.bigquery.app import DEFAULT_PORT as BIGQUERY_PORT
        from crucible.server.snowflake.app import DEFAULT_PORT as SNOWFLAKE_PORT
    except ImportError as exc:  # server extra not installed
        print(f"crucible serve needs: pip install 'crucible-emulator[server]' ({exc})")
        return 2
    port = args.port or (BIGQUERY_PORT if args.dialect == "bigquery" else SNOWFLAKE_PORT)
    engine = Engine(args.database)
    # Access logs off: request lines are noise here, and must never carry credentials (ADR 0006).
    uvicorn.run(create_app(args.dialect, engine), host=args.host, port=port, access_log=False)
    return 0


def _corpus(args: argparse.Namespace) -> int:
    from crucible.corpus.cli import main as corpus_main

    return corpus_main(args)


def _mirror_schema(args: argparse.Namespace) -> int:
    try:
        from google.api_core.client_options import ClientOptions
        from google.api_core.exceptions import GoogleAPICallError
        from google.auth.credentials import AnonymousCredentials
        from google.auth.exceptions import GoogleAuthError
        from google.cloud import bigquery

        from crucible.mirror import mirror_schema
    except ImportError as exc:
        extra = "pip install 'crucible-emulator[record-bigquery]'"
        print(f"crucible mirror-schema needs: {extra} ({exc})", file=sys.stderr)
        return 2
    to_project = args.to_project or args.from_project

    def warn(message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)

    try:
        source = bigquery.Client(project=args.from_project)  # Application Default Credentials
        target = bigquery.Client(
            project=to_project,
            credentials=AnonymousCredentials(),  # pyright: ignore[reportArgumentType]
            client_options=ClientOptions(api_endpoint=args.to),
        )
        summary = mirror_schema(source, target, args.from_project, to_project, args.dataset, warn)
    except (GoogleAPICallError, GoogleAuthError, OSError) as exc:
        print(f"crucible mirror-schema stopped: {exc}", file=sys.stderr)
        return 1
    print(
        f"mirrored {summary.datasets} datasets, {summary.tables} tables, {summary.views} views; "
        f"skipped {summary.skipped}"
    )
    return 0 if summary.tables or summary.views else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the emulator server")
    serve.add_argument("--dialect", choices=DIALECTS, required=True)
    serve.add_argument(
        "--host",
        # Loopback by default; the Docker image sets CRUCIBLE_HOST=0.0.0.0 (ADR 0006).
        default=os.environ.get("CRUCIBLE_HOST", "127.0.0.1"),
        help="bind address (default: $CRUCIBLE_HOST or 127.0.0.1)",
    )
    serve.add_argument("--port", type=int, help="default: 9050 (bigquery), 8080 (snowflake)")
    serve.add_argument("--database", help="DuckDB file for persistent state (default: in memory)")
    serve.set_defaults(func=_serve)

    corpus = sub.add_parser("corpus", help="run or record differential corpus cases")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    run = corpus_sub.add_parser("run", help="run cases against Crucible (no warehouse access)")
    run.add_argument("--dialect", choices=DIALECTS, required=True)
    run.add_argument("--path", help="case file or directory (default: corpus/<dialect>)")
    run.add_argument(
        "--wire",
        metavar="URL",
        help="run every case through the official client against a running Crucible server",
    )
    record = corpus_sub.add_parser(
        "record", help="run cases against the REAL warehouse and write expected results"
    )
    record.add_argument("--dialect", choices=DIALECTS, required=True)
    record.add_argument("--path", help="case file or directory (default: corpus/<dialect>)")
    record.add_argument("--max-bytes-billed", type=int, required=True)
    record.add_argument(
        "--unverified-only",
        action="store_true",
        help="record only cases whose verified flag is false",
    )
    record.add_argument(
        "--i-understand-this-spends-credits",
        dest="confirmed",
        action="store_true",
        help="required: record runs every case against the real warehouse",
    )
    record_api = corpus_sub.add_parser(
        "record-api",
        help="record raw BigQuery REST responses as redacted wire fixtures (spends credits)",
    )
    record_api.add_argument("--dialect", choices=("bigquery",), default="bigquery")
    record_api.add_argument("--out", default="tests/fixtures/bigquery_api")
    record_api.add_argument("--max-bytes-billed", type=int, required=True)
    record_api.add_argument(
        "--i-understand-this-spends-credits",
        dest="confirmed",
        action="store_true",
        help="required: record-api runs jobs against the real warehouse",
    )
    corpus.set_defaults(func=_corpus)

    mirror = sub.add_parser(
        "mirror-schema",
        help="copy table and view schemas (no data) from BigQuery into Crucible",
    )
    mirror.add_argument("--from-project", required=True, help="BigQuery project to read (ADC)")
    mirror.add_argument(
        "--dataset",
        action="append",
        required=True,
        help="dataset to mirror; repeat for several (there is no all-datasets mode)",
    )
    mirror.add_argument(
        "--to",
        default="http://127.0.0.1:9050",
        help="Crucible BigQuery endpoint (default: %(default)s)",
    )
    mirror.add_argument(
        "--to-project", help="project to create in Crucible (default: --from-project)"
    )
    mirror.set_defaults(func=_mirror_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
