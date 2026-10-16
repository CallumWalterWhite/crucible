"""02 — Datasets, tables, INSERT and SELECT through the emulator.

Start the emulator first:

    uv run crucible serve --dialect bigquery --port 9050

Run:

    uv run --with google-cloud-bigquery examples/bigquery/02_tables.py

Creates a dataset + table, inserts rows, reads them back, then cleans up
so re-runs start fresh.
"""

from __future__ import annotations

import os
import uuid

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

ENDPOINT = os.environ.get("CRUCIBLE_URL", "http://127.0.0.1:9050")
PROJECT = "local-project"


def main() -> None:
    client = bigquery.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": ENDPOINT},
    )
    dataset_id = f"example_{uuid.uuid4().hex[:8]}"
    table_ref = f"{PROJECT}.{dataset_id}.people"

    client.create_dataset(bigquery.Dataset(f"{PROJECT}.{dataset_id}"))
    try:
        client.create_table(
            bigquery.Table(
                table_ref,
                schema=[
                    bigquery.SchemaField("id", "INTEGER"),
                    bigquery.SchemaField("name", "STRING"),
                ],
            )
        )

        insert = client.query(
            f"INSERT INTO `{table_ref}` (id, name) VALUES (1, 'ada'), (2, 'grace')"
        )
        insert.result()
        print("inserted rows:", insert.num_dml_affected_rows)

        rows = client.query(f"SELECT id, name FROM `{table_ref}` ORDER BY id").result()
        for row in rows:
            print(row.id, row.name)

        table = client.get_table(table_ref)
        print("num_rows:", table.num_rows)
        print("tables:", [t.table_id for t in client.list_tables(f"{PROJECT}.{dataset_id}")])
    finally:
        client.delete_dataset(f"{PROJECT}.{dataset_id}", delete_contents=True, not_found_ok=True)
        print("cleaned up", dataset_id)


if __name__ == "__main__":
    main()
