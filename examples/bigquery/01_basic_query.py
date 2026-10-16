"""01 — Connect to the BigQuery emulator and run a query.

Start the emulator first (BigQuery on :9050):

    uv run crucible serve --dialect bigquery --port 9050

Then run:

    uv run --with google-cloud-bigquery examples/bigquery/01_basic_query.py
    CRUCIBLE_URL=http://localhost:9050 uv run --with google-cloud-bigquery \
        examples/bigquery/01_basic_query.py   # explicit endpoint

Every snippet points at the emulator only: AnonymousCredentials (no ADC
lookup) plus client_options api_endpoint. Nothing reaches real BigQuery.
"""

from __future__ import annotations

import os

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

ENDPOINT = os.environ.get("CRUCIBLE_URL", "http://127.0.0.1:9050")
PROJECT = "local-project"


def make_client() -> bigquery.Client:
    return bigquery.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": ENDPOINT},
    )


def main() -> None:
    client = make_client()

    # Simple query: client.query() POSTs jobs.insert; .result() polls until DONE.
    rows = list(client.query("SELECT 1 AS id, 'ada' AS name").result())
    for row in rows:
        print(dict(row.items()))

    # query_and_wait() POSTs jobs.query instead — same emulator, other entrypoint.
    for row in client.query_and_wait("SELECT 2 + 2 AS answer"):
        print(dict(row.items()))

    # Paging: page_size drives maxResults on jobs.getQueryResults.
    job = client.query("SELECT x FROM UNNEST(GENERATE_ARRAY(1, 25)) AS x ORDER BY x")
    pages = list(job.result(page_size=10).pages)
    print(f"{len(pages)} pages:", [row.x for page in pages for row in page])


if __name__ == "__main__":
    main()
