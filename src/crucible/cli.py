"""`crucible serve`, `crucible corpus run|record`."""

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
    record = corpus_sub.add_parser(
        "record", help="run cases against the REAL warehouse and write expected results"
    )
    record.add_argument("--dialect", choices=DIALECTS, required=True)
    record.add_argument("--path", help="case file or directory (default: corpus/<dialect>)")
    record.add_argument("--max-bytes-billed", type=int, required=True)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
