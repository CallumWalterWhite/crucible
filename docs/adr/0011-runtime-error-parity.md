# 11. Runtime error parity

Status: accepted

## Context

Phase 0 found cases where BigQuery raises an error and DuckDB returns a value. A test that passes
on Crucible and fails in the warehouse is the worst outcome for a development emulator.

## Decision

Crucible raises wherever BigQuery raises, whenever the condition is detectable at run time. There
is no lenient mode. The five known cases are all emulated:

| Case | BigQuery | Crucible |
|---|---|---|
| invalid CAST (e.g. `CAST('1.5' AS INT64)`) | error | error |
| division by zero (`x / 0`) | error | error |
| MERGE where a target row matches more than one source row | error | error |
| NULL element in an `ARRAY_AGG` result | error | error |
| `OFFSET(n)` / `ORDINAL(n)` out of range | error | error |

- The generated SQL raises through DuckDB's `error()` with the `CRUCIBLE:RUNTIME:` prefix; the
  engine turns that into `ErrorKind.RUNTIME` with the warehouse's message text
  (`engine/errors.py`).
- `SAFE.`-prefixed functions and `SAFE_CAST`, `SAFE_DIVIDE`, `SAFE_OFFSET`, `SAFE_ORDINAL`
  suppress the error and return NULL, as in BigQuery. The `SAFE.` prefix fix and these checks are
  built together, because they share the same rewrite.
- Conditions Crucible cannot detect are listed in `docs/compatibility.md` as known false passes.

## Consequences

- Generated SQL is longer and slower; correctness wins (project principle).
- Each check has a corpus case for the error and for its SAFE variant.
