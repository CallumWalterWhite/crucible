# 14. Type mapping and canonical values

Status: accepted (amended in Phase 2: output types)

## Context

The engine's canonical type model is SQLGlot's `DataType` in the DuckDB dialect (`engine/types.py`).
Each dialect maps warehouse types to DuckDB storage types for DDL and casts, and maps DuckDB result
types back to warehouse types for responses and the corpus. A mapping that loses precision or
meaning is a silent divergence, so it is either exact or an explicit error.

## Decision

BigQuery mapping (`dialects/bigquery/types.py`):

| BigQuery | DuckDB | Notes |
|---|---|---|
| INT64 | BIGINT | |
| FLOAT64 | DOUBLE | |
| NUMERIC / DECIMAL | DECIMAL(38,9) | always explicit; SQLGlot's bare `DECIMAL` is DECIMAL(18,3) in DuckDB |
| NUMERIC(P,S) | DECIMAL(P,S) | P ≤ 38 |
| BIGNUMERIC / BIGDECIMAL | — | raises `CrucibleUnsupportedError` (precision 76 exceeds DuckDB's 38) |
| BOOL | BOOLEAN | |
| STRING | VARCHAR | |
| BYTES | BLOB | |
| DATE | DATE | |
| DATETIME | TIMESTAMP | |
| TIME | TIME | |
| TIMESTAMP | TIMESTAMP WITH TIME ZONE | |
| JSON | JSON | |
| ARRAY\<T\> | T[] | |
| STRUCT\<a T, ...\> | STRUCT(a T, ...) | field order kept |
| INTERVAL | INTERVAL | canonical rendering not defined yet: raises `CrucibleUnsupportedError` in the corpus |
| GEOGRAPHY, RANGE | — | raise `CrucibleUnsupportedError` |

### Output types come from the BigQuery AST (Phase 2 amendment)

DuckDB types results by its own rules, and they differ from BigQuery's on ordinary SQL (verified on
DuckDB 1.5.6): `SUM(BIGINT)` is HUGEINT; the literal `2.5` is `DECIMAL(2,1)` where BigQuery types it
FLOAT64, which changes ROUND results and COALESCE/IF unification; `DECIMAL(38,9) * DECIMAL(38,9)` is
`DECIMAL(38,18)` where BigQuery keeps NUMERIC. Mapping DuckDB result types back to BigQuery types
cannot recover the right answer, so:

- **Projection types.** Each output column's warehouse type is the type SQLGlot's `annotate_types`
  gives that projection in the qualified BigQuery AST (ADR 0010).
- **Casts.** The generator wraps every top-level projection in a CAST to the DuckDB storage type of
  its warehouse type (the table above). This covers SELECT results and the columns that CTAS,
  INSERT and MERGE store. The CAST is also BigQuery's overflow check: a HUGEINT sum that does not
  fit INT64 raises DuckDB's conversion error, which maps to a runtime error.
- **Literals** are typed by BigQuery rules in a transform before generation; for example a float
  literal becomes a DOUBLE.
- **Fallback.** The reverse mapping (the inverse of the table) is used only when annotation returns
  UNKNOWN. A DuckDB type with no row in the table (HUGEINT, UBIGINT, ...) then raises
  `CrucibleUnsupportedError` naming the expression, rather than being reported under a guessed
  warehouse type.
- **Measured.** The share of projections in a real dbt project that annotation leaves UNKNOWN, and the
  functions that cause it, decide whether the dialect needs its own per-function type rules early
  in Phase 2.

Canonical values for BigQuery (`Dialect.canonical_value`, used only by the corpus):

| Type | Canonical JSON value |
|---|---|
| NULL (any type) | `null` |
| INT64 | integer |
| FLOAT64 | number; `"NaN"`, `"Infinity"`, `"-Infinity"` as strings |
| NUMERIC | string, plain decimal, no exponent, trailing fractional zeros removed (`"2.35"`, `"3"`, `"-0.5"`) |
| BOOL | `true` / `false` |
| STRING | string |
| BYTES | base64 string |
| DATE | `"YYYY-MM-DD"` |
| DATETIME | `"YYYY-MM-DDTHH:MM:SS[.ffffff]"` (fraction only when non-zero, 6 digits) |
| TIME | `"HH:MM:SS[.ffffff]"` |
| TIMESTAMP | `"YYYY-MM-DDTHH:MM:SS[.ffffff]Z"` in UTC |
| JSON | the parsed JSON value (objects compare without key order) |
| ARRAY | list of canonical elements |
| STRUCT | mapping of field name to canonical value |

## Consequences

- Corpus files are readable and diff cleanly; exact decimals never pass through floats.
- New warehouse types need a row here before they work anywhere.
