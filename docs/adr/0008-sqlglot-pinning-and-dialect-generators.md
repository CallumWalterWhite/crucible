# 8. SQLGlot and DuckDB pinning, and per-source-dialect generators

Status: accepted

## Context

SQLGlot parses warehouse SQL and generates DuckDB SQL. Phase 0 found SQLGlot 30.22.0 silently
mistranslating several BigQuery constructs (e.g. the `SAFE.` prefix, `JSON_TYPE` values,
`REGEXP_EXTRACT` with no match, `NUMERIC` precision). The right DuckDB SQL for a construct also
depends on the source warehouse: Snowflake's `TYPEOF` returns upper case, its `DATEDIFF` counts
boundaries, and its default timezone is America/Los_Angeles.

## Decision

- Pin SQLGlot to an exact version (`sqlglot==30.22.0`). Upgrading is a deliberate change that runs
  the full corpus.
- Pin DuckDB to an exact version (`duckdb==1.5.6`) for the same reason: function results, result
  types and reserved words change between DuckDB releases (for example the result type of
  `SUM(BIGINT)` or a new keyword that collides with a column name). A DuckDB upgrade runs the full
  corpus, like a SQLGlot upgrade, and Dependabot proposes both as separate pull requests.
- Each source dialect owns a generator: `dialects/bigquery/generator.py` subclasses SQLGlot's
  DuckDB generator, and Snowflake gets its own in Phase 5. There is no shared DuckDB generator; a
  fix written for one source warehouse never changes another's output.
- Every translation fix lives in that dialect's transforms or generator, with a unit test and a
  corpus case.
- Fixes that are SQLGlot bugs for every user (for example the `ARRAY_AGG ... IGNORE NULLS ...
  ORDER BY` FILTER bug) are submitted upstream. The local workaround stays, with a comment linking
  the upstream PR, until a pinned SQLGlot release contains the fix.

## Consequences

- Translation behaviour changes only when Crucible's code or the pin changes.
- Some duplication between dialect generators is accepted in exchange for isolation.
