# 10. Schema-aware translation

Status: accepted

## Context

Many correct translations depend on types: `SAFE_CAST` targets, division by zero checks, implicit
struct field expansion in unaliased `UNNEST`, name resolution between `UNNEST` aliases and joined
columns, `DATE_TRUNC` result types. Text-level rewrites cannot see types.

## Decision

Every SQL statement is translated in five steps, inside the dialect:

1. **parse** the source SQL with SQLGlot's source dialect;
2. **qualify** it against the live catalog schema, so every column has its table and every table
   its catalog and schema (`sqlglot.optimizer.qualify` with a schema built from `ExecContext.catalog`);
3. **annotate types** (`sqlglot.optimizer.annotate_types`);
4. apply the dialect's **transforms** (type-aware rewrites, runtime error checks, unsupported
   checks);
5. **generate** DuckDB SQL with the dialect's generator (ADR 0008), quoting every identifier.

The Dialect protocol passes the schema explicitly: `Dialect.plan(statement, ctx)` receives the
`ExecContext`, whose `catalog` is the live schema bound to the request's cursor, so it also sees
tables created earlier in the same transaction or script. The dialect builds the SQLGlot schema
from only the tables the statement references, mapping DuckDB column types to warehouse types
(ADR 0014) so type annotation uses the source warehouse's rules.

## Consequences

- A reference to a missing table or column fails at qualify time with the warehouse's not-found or
  invalid-query error, before DuckDB runs anything.
- Every generated identifier is quoted, so warehouse names that are reserved words in DuckDB
  (for example `similar`) work.
