# 13. Corpus format and recording

Status: accepted

## Context

Fidelity is measured, not claimed. Every behaviour Crucible emulates needs a case that states the
warehouse's real answer, and that answer must come from the warehouse, not from a person.

## Decision

A corpus case is one YAML file at `corpus/<dialect>/<area>/<case>.yaml`:

```yaml
description: SAFE_DIVIDE returns NULL when dividing by zero
tags: [functions, math]
setup:                      # source-dialect statements, run in order (optional)
  - CREATE TABLE t (a INT64, b INT64)
  - INSERT INTO t VALUES (1, 0), (4, 2)
query: SELECT SAFE_DIVIDE(a, b) AS q FROM t
ordered: false              # default false: rows compare as a multiset
expected:                   # either columns + rows ...
  columns:
    - {name: q, type: FLOAT64}
  rows:
    - [null]
    - [2.0]
# expected:                 # ... or an error
#   error: {kind: runtime, message_contains: "division by zero", message: "..."}
verified: false             # written only by `crucible corpus record`
recorded_at: null           # ISO-8601 UTC, written only by `crucible corpus record`
```

- **Names.** Setup and query use unqualified table names. They resolve in an isolated namespace
  per case: a fresh in-memory engine with default catalog `crucible-corpus` and default schema
  `corpus` locally, and a temporary dataset `crucible_corpus_<random>` when recording.
- **Types** are warehouse type names as the dialect renders them (`Dialect.warehouse_type`), e.g.
  `INT64`, `ARRAY<STRING>`, `STRUCT<a INT64, b STRING>`.
- **Values** are canonical, JSON-safe renderings from `Dialect.canonical_value(value, type)`. The
  same function renders values from Crucible and from the warehouse client, so encoding
  differences cannot hide or invent a mismatch.
- **Errors.** An expected error states which error: `kind` (a Crucible `ErrorKind`: `invalid_query`,
  `not_found`, `already_exists`, `conflict` or `runtime`) and a non-empty `message_contains`. The
  loader rejects `error: {}` and errors without either key. A case passes only when Crucible raises
  that kind and its message contains `message_contains`, so a translation bug that surfaces as a
  DuckDB binder error (`invalid_query`) cannot pass for a runtime check. `message_contains` comes
  from BigQuery's documented message until a recording supplies the real one. Recording stores
  the warehouse's verbatim message as `message`.
- **Verification.** `verified: true` and `recorded_at` are set only by `crucible corpus record`, which
  runs the case against the real warehouse. Nobody sets them by hand. CI enforces it: on pull
  requests, `scripts/check-corpus-verified.sh` fails when a `corpus/` YAML line changes to
  `verified: true` on a branch that does not start with `corpus-record/` (the record workflow's
  branch).
- **Error mismatches during recording.** When the warehouse's message does not contain the case's
  `message_contains`, record writes the verbatim `message` and `recorded_at`, keeps `kind` and
  `message_contains` as written, leaves `verified: false`, and continues. It lists those cases at the
  end for a person to choose a new substring. Recording never invents a substring.
- **Temporary datasets.** Each case runs in a dataset created with `default_table_expiration_ms` of
  one hour and the label `crucible-corpus=1`, and record deletes it when the case ends. The
  expiration and label only matter if the process dies before the delete.
- **Recording spends credits.** `crucible corpus record` requires `--max-bytes-billed` (applied to every
  BigQuery job) and `--i-understand-this-spends-credits`. It never runs in normal CI; only the
  manual `corpus-record` workflow or a maintainer runs it, and only when asked.
- **Running.** `crucible corpus run` and `tests/corpus` run every case against Crucible. Unverified
  cases run too; reports show verified and unverified separately.

## Consequences

- A case is portable between the local engine and the warehouse because both use the same
  namespace rules and the same canonical renderer.
- Record rewrites only `expected`, `verified` and `recorded_at`; YAML comments are not preserved.
- An expected-error case is as strict as a value case: right kind, right message.
- Snowflake recording arrives with the Snowflake dialect (Phase 5).
