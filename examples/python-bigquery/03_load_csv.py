"""03 — CSV seed via a resumable-upload load job.

Start the emulator first:

    uv run crucible serve --dialect bigquery --port 9050

Run:

    uv run --with google-cloud-bigquery examples/bigquery/03_load_csv.py

Mirrors what dbt seeds do: explicit schema, skip_leading_rows=1,
WRITE_TRUNCATE. Only the CSV spellings the recorded fixtures confirm are
accepted — anything else raises unsupported.
"""

from __future__ import annotations

import io
import os
import uuid

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

ENDPOINT = os.environ.get("CRUCIBLE_URL", "http://127.0.0.1:9050")
PROJECT = "local-project"

CSVPAYLOAD = (
    b"id,name,day,amount,active,note\n"
    b"1,ada,2024-01-02,1.5,true,\n"
    b"2,grace,2024-02-29,10.0,false,hi\n"
)


def main() -> None:
    client = bigquery.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": ENDPOINT},
    )
    dataset_id = f"example_{uuid.uuid4().hex[:8]}"
    table_ref = f"{PROJECT}.{dataset_id}.seeded"

    client.create_dataset(bigquery.Dataset(f"{PROJECT}.{dataset_id}"))
    try:
        config = bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("id", "INTEGER"),
                bigquery.SchemaField("name", "STRING"),
                bigquery.SchemaField("day", "DATE"),
                bigquery.SchemaField("amount", "FLOAT"),
                bigquery.SchemaField("active", "BOOLEAN"),
                bigquery.SchemaField("note", "STRING"),
            ],
            source_format="CSV",
            skip_leading_rows=1,
            write_disposition="WRITE_TRUNCATE",
        )
        client.load_table_from_file(io.BytesIO(CSVPAYLOAD), table_ref, job_config=config).result()

        rows = client.query(
            f"SELECT id, name, day, amount, active, note FROM `{table_ref}` ORDER BY id"
        ).result()
        for row in rows:
            print(tuple(row.values()))
    finally:
        client.delete_dataset(f"{PROJECT}.{dataset_id}", delete_contents=True, not_found_ok=True)
        print("cleaned up", dataset_id)


if __name__ == "__main__":
    main()
