# 4. BigQuery jobs execute synchronously at submit

Status: accepted

## Context

BigQuery jobs are asynchronous: clients insert a job, poll it, then page through results.
Emulating real asynchrony adds state and timing without improving fidelity for development.

## Decision

- `jobs.insert` and `jobs.query` run the job to completion before responding. The job is stored in
  an in-memory job store with its status (`DONE`), statistics, schema and rows.
- `jobs.get` and `getQueryResults` answer from the store, with `pageToken`/`maxResults` paging.
- Jobs carry the client's `jobId`. Inserting an existing `jobId` returns 409 Conflict, which
  dbt-bigquery handles by attaching with `jobs.get` (dbt-bigquery 1.12.1
  `connections.py:236-251`).
- **Anonymous result tables.** BigQuery writes every SELECT result to an anonymous destination
  table, and dbt calls `tables.get` on it. The job store answers `tables.get` for these tables,
  without creating them in DuckDB. dbt needs more than the row count here: it reads `num_rows` for
  run messages (`connections.py:368,383`) and the **schema** in `get_columns_in_select_sql`, which
  unit tests and contracts use (`impl.py:613`). Both come from the stored job result.
  `tabledata.list` on an anonymous table also reads from the store.
- The store is per process and is cleared by `POST /_crucible/reset`.

## Consequences

- Responses are deterministic; polling clients see `DONE` on the first poll.
- Results live in memory; very large results are out of scope (no performance work).
