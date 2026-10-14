# 1. One dialect per server process

Status: accepted

## Context

BigQuery and Snowflake differ in namespaces, identifier case, session state, type names, error
formats and wire protocols. Serving both from one process would force shared state with two sets
of rules.

## Decision

`crucible serve --dialect bigquery|snowflake` starts a server for exactly one warehouse. The
process holds one `Engine` (one DuckDB database), one dialect, and that dialect's server. There is
no state shared across dialects. A user who needs both runs two processes or two containers.

## Consequences

- Each dialect owns its rules completely; the engine stays warehouse-agnostic.
- The Docker image is the same for both; the dialect is a command-line argument.
- `import-linter` enforces that `crucible.dialects.bigquery` and `crucible.dialects.snowflake` never
  import each other.
