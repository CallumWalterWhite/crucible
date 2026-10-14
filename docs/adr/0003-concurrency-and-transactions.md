# 3. Concurrency and transactions

Status: accepted

## Context

dbt runs models in parallel (dbt projects commonly run `threads: 8`). DuckDB supports many connections to one
database in one process, with optimistic concurrency control.

## Decision

- One process owns the DuckDB database. Each request gets its own cursor (`Engine.cursor`), which
  is a DuckDB connection sharing the database, and the cursor closes when the request ends.
- An explicit transaction pins its cursor to the session (`Session.transaction`) until COMMIT or
  ROLLBACK, so a Snowflake session can span requests. A BigQuery multi-statement transaction lives
  inside one script request. A session lock serialises requests that share a pinned cursor.
- DuckDB write conflicts (`TransactionException: Conflict ...`) become `ErrorKind.CONFLICT`, which
  each server maps to the warehouse's concurrency error.
- The unit suite runs 8 threads of concurrent inserts and reads; the dbt e2e suite runs with
  `threads: 4` or more.

## Consequences

- Concurrent dbt models that write different tables never conflict; two writers to one table get
  the warehouse's own retryable error rather than a hang or corruption.
