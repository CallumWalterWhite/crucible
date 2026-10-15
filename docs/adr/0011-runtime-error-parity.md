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

## Implementation (Phase 2)

- Checks are rewrites in `dialects/bigquery/transforms/errors.py`, applied bottom-up so nested cases
  are checked (`(1 / 0) / 2` raises).
- `SAFE.f(args)` wraps only `f` in DuckDB's `TRY`: arguments that could raise are evaluated outside
  it, through `list_extract(list_transform([struct_pack(...)], s -> TRY(f(s.a0, ...))), 1)`, so
  `SAFE.LOG(1 / 0)` still raises division by zero while `SAFE.LOG(-1)` is NULL.
- Arrays in written results (query results, CTAS, INSERT, MERGE) follow BigQuery's write rules, in
  `translate.finalize_value`: a NULL element raises "Array cannot have a null element; error in
  writing field <name>", and a NULL array is written as `[]`. Inside a query, arrays keep NULLs.
- Errors DuckDB raises itself are reworded where BigQuery's text is known
  (`dialects/bigquery/errors.reword`): INT64 overflow becomes "integer overflow (...)".
- MERGE runs a duplicate-match check in the same transaction as the MERGE when it has a WHEN
  MATCHED clause ("UPDATE/MERGE must match at most one source row for each target row").

## Consequences

- Generated SQL is longer and slower; correctness wins (project principle).
- Each check has a corpus case for the error and for its SAFE variant.
