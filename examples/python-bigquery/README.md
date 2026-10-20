# BigQuery examples (google-cloud-bigquery)

Showcase for the Crucible BigQuery emulator. Every script talks to the
local emulator only — anonymous credentials plus an endpoint override —
so nothing reaches real BigQuery and queries cost nothing.

## Setup

```sh
uv sync --all-extras          # serve needs the server extra
uv run crucible serve --dialect bigquery --port 9050
```

Each script defaults to `http://127.0.0.1:9050`; override with `CRUCIBLE_URL`:

```sh
CRUCIBLE_URL=http://localhost:9050 uv run --with google-cloud-bigquery \
    examples/bigquery/01_basic_query.py
```

The client needs `google-cloud-bigquery` (any `>=3.27`). Do not install
`google-cloud-bigquery-storage`, or pass `create_bqstorage_client=False`
to `to_arrow`/`to_dataframe` — the Storage API is gRPC on another host
and out of scope for this REST emulator (see `docs/client-compat.md`).

## Scripts

| Script | Shows |
|---|---|
| `01_basic_query.py` | Connect, `query()` + `query_and_wait()`, paged results |
| `02_tables.py` | Create dataset/table, `INSERT`, `SELECT`, `get_table`/`list_tables`, cleanup |
| `03_load_csv.py` | CSV seed via a resumable-upload load job (like dbt seeds) |
| `04_errors.py` | Explicit errors: unsupported (INTERVAL), division by zero, missing table, syntax error |

Scripts 02–03 create uniquely named datasets and delete them on exit, so
re-runs start fresh.
