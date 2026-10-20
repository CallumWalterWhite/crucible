"""04 — Errors are explicit, never silently wrong.

Start the emulator first:

    uv run crucible serve --dialect bigquery --port 9050

Run:

    uv run --with google-cloud-bigquery examples/bigquery/04_errors.py

Anything Crucible cannot match exactly raises CrucibleUnsupportedError
inside, which the server maps to the warehouse error shape (BadRequest
here). Runtime errors (division by zero), missing tables (NotFound) and
syntax errors (BadRequest) map the same way.
"""

from __future__ import annotations

import os

from google.api_core import exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

ENDPOINT = os.environ.get("CRUCIBLE_URL", "http://127.0.0.1:9050")
PROJECT = "local-project"


def show(label: str, sql: str) -> None:
    client = bigquery.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": ENDPOINT},
    )
    try:
        list(client.query(sql).result())
    except exceptions.GoogleAPICallError as exc:
        print(f"{label}: {type(exc).__name__}: {exc.message}")
    else:
        print(f"{label}: unexpectedly succeeded")


def main() -> None:
    show("unsupported (INTERVAL)", "SELECT INTERVAL 1 DAY AS i")
    show("runtime (division by zero)", "SELECT 1/0 AS v")
    show("missing table", "SELECT * FROM `local-project.nope.missing`")
    show("syntax error", "SELEC 1")


if __name__ == "__main__":
    main()
