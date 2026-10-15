# Architecture

Crucible is a local server that speaks the BigQuery or Snowflake API and runs queries on DuckDB.
A client (an official SDK, dbt with its standard adapter, a script, a BI tool) changes only its
endpoint and uses dummy credentials. This page describes how a request flows through Crucible and
which component owns what. Each decision has an ADR in [`docs/adr/`](adr/).

Principles:

- **Correctness over performance.** Slow and correct is fine; silently wrong is not.
- **Unsupported means an explicit error.** Crucible never emits a best guess (ADR 0009).
- **Fidelity is measured, not claimed.** The differential corpus proves every behaviour (ADR 0013).

## Layers

```
crucible.cli
    ├── crucible.server      (Starlette app, one dialect server per process)
    └── crucible.corpus      (corpus runner and recorder)
crucible.dialects            (bigquery, snowflake: translation, types, metadata, commands)
crucible.engine              (warehouse-agnostic kernel on DuckDB)
```

A layer imports only the layers below it. `server` and `corpus` are siblings and do not import
each other, and the two dialects never import each other. `import-linter` enforces both rules in
CI (`pyproject.toml`, `[tool.importlinter]`). The engine refers to the `Dialect` protocol only for
type checking.

## Request pipeline

```
client
  -> server/<dialect>      decode the API request, find or create the session
  -> engine.Pipeline       dialect.split(sql) -> statements
       for each statement: dialect.plan(statement, ctx) -> SqlPlan | CommandPlan
         SqlPlan:     DuckDB SQL from the dialect's translation, run on the request's cursor
         CommandPlan: a handler (SHOW / USE / DESCRIBE / ALTER SESSION, DDL side effects,
                      scripts, which call ctx.execute() for each child statement)
  -> engine.Result         columns (DuckDB types), rows, rowcount, statement type
  -> dialect types         DuckDB types -> warehouse types (ADR 0014)
  -> server/<dialect>      encode the wire response; map CrucibleError to the warehouse error
```

Translation inside `dialect.plan` is schema-aware (ADR 0010): parse, qualify against the live
catalog, annotate types, apply the dialect's transforms, then generate DuckDB SQL with the
dialect's own generator (ADR 0008), quoting every identifier.

## Components

### `crucible.engine`

| Module | Owns |
|---|---|
| `engine.py` | the single DuckDB database: hardening and configuration lock (ADR 0006), ATTACHed catalogs (ADR 0002), storage layout (ADR 0005), a cursor per request (ADR 0003), reset and health |
| `catalog.py` | namespace operations bound to a cursor: catalogs, schemas, tables and views with columns, and per-object properties for metadata DuckDB cannot hold |
| `session.py` | session state: default catalog and schema, timezone (ADR 0012), variables, dialect state, the pinned transaction cursor |
| `pipeline.py` | `Pipeline`, `ExecContext` (what a dialect sees: session, cursor, live catalog, params), `SqlPlan`, `CommandPlan`, `Result` |
| `types.py` | the canonical type model: SQLGlot `DataType` in the DuckDB dialect, and `Column` |
| `errors.py` | `CrucibleError` with an `ErrorKind`, `CrucibleUnsupportedError`, classification of DuckDB exceptions, and the runtime-error prefix generated SQL uses (ADR 0011) |

The engine knows nothing about any warehouse. It runs DuckDB SQL and commands that dialects hand
it.

### `crucible.dialects`

`dialects/base.py` defines the protocol, the only contract the engine knows:

```python
class Dialect(Protocol):
    name: str

    def new_session(self, default_catalog=None, default_schema=None) -> Session: ...
    def split(self, sql: str) -> list[object]: ...
    def plan(self, statement: object, ctx: ExecContext) -> SqlPlan | CommandPlan: ...
    def warehouse_type(self, duckdb_type: exp.DataType) -> str: ...
    def canonical_value(self, value: Any, warehouse_type: str) -> Any: ...
```

Each dialect package has the same modules:

| Module | Owns |
|---|---|
| `statements.py` | splitting a request into statements and routing each to translation, a command, or the script interpreter; CREATE TABLE and INSERT (with the warehouse's assignment coercion rules) |
| `translate.py` | the translation pipeline for queries: resolve names, qualify, annotate, fix annotation, transforms, storage casts, projection casts, generate |
| `transforms/` | type-aware AST rewrites, one module per area (below) |
| `generator.py` | the dialect's subclass of SQLGlot's DuckDB generator (ADR 0008) |
| `types.py` | type mapping both ways, coercion and supertypes, and canonical values (ADR 0014) |
| `metadata.py` | identifier case rules and name resolution (ADR 0002), INFORMATION_SCHEMA |
| `commands.py` / `scripting.py` | Snowflake commands (SHOW, USE, ALTER SESSION, ...) / BigQuery scripting (DECLARE, SET, IF, BEGIN...END, transactions) |
| `errors.py` | warehouse error messages for errors Crucible detects itself |
| `dialect.py` | `DIALECT`, the protocol implementation; found by `crucible.dialects.get_dialect` |

### Transforms

`dialects/<d>/transforms/` is a package with one module per area, so two people never edit the
same file. For BigQuery: `unnest`, `json`, `regexp`, `strings`, `dates`, `arrays`, `errors`,
`safe`, `literals`; `common.py` holds the helpers that raise runtime and unsupported errors from
generated SQL. Each module exposes `transform(tree, ctx) -> tree`; `transforms/__init__.py` holds the
only ordered list (`PIPELINE`). Transforms run on the qualified and type-annotated AST, keep `.type`
on what they build, and raise `CrucibleUnsupportedError` for anything in their area they cannot
translate exactly. Each transform has unit tests and corpus cases.

### Output types (ADR 0014)

`translate.py` takes each output column's warehouse type from the annotated AST, after
`fix_annotation` corrects types SQLGlot gets wrong (for example `array || array` typed STRING) and
normalises type spellings. Set operations use the branches' supertype. Every CAST target becomes its
storage type (NUMERIC is DECIMAL(38,9)), and every top-level projection is cast to the storage type
of its warehouse type, which also performs the warehouse's overflow check.

### `crucible.server`

`server/app.py` builds a Starlette app (ADR 0007) for one dialect (ADR 0001) with admin routes
under `/_crucible/` (health, reset) and a catch-all that returns the warehouse's
"not implemented in Crucible: <endpoint>" error. Every response, including 404, 405 and 500, has
the warehouse's shape.

- `server/bigquery/`: REST v2 routes, the job store (ADR 0004), pagination, load jobs and uploads,
  JSON encoding.
- `server/snowflake/`: login and session, query requests, result retrieval, JSON result encoding.

### `crucible.corpus` and `crucible.cli`

`crucible serve`, `crucible corpus run`, and `crucible corpus record` (ADR 0013). Record runs cases
against the real warehouse; it requires `--max-bytes-billed` and
`--i-understand-this-spends-credits`, and only maintainers run it.

## BigQuery API subset (Phase 3)

Phase 0 read dbt-bigquery 1.12.1 and google-cloud-bigquery 3.45 to find the calls the standard
dbt materializations and seeds make. Phase 3 implements exactly these; everything else returns
the not-implemented error.

| Call | REST | Used for |
|---|---|---|
| `jobs.insert` (client-minted `jobId`; duplicate → 409) | `POST /projects/{p}/jobs` | every SQL statement |
| `jobs.get` | `GET /projects/{p}/jobs/{id}` | polling, 409 attach |
| `jobs.getQueryResults` | `GET /projects/{p}/queries/{id}` | rows and paging |
| `jobs.query` | `POST /projects/{p}/queries` | `query_and_wait` in recent clients |
| `tables.get` | `GET .../datasets/{d}/tables/{t}` | columns, row counts, anonymous result tables (ADR 0004) |
| `tables.list` | `GET .../datasets/{d}/tables` | dbt relation cache |
| `tables.patch` | `PATCH .../datasets/{d}/tables/{t}` | `on_schema_change: append_new_columns` |
| `tables.delete` | `DELETE .../datasets/{d}/tables/{t}` | `drop_relation` |
| `routines.list` / `routines.get` | `GET .../routines[/{r}]` | dbt relation cache (empty) |
| `datasets.list` / `datasets.get` | `GET /projects/{p}/datasets[/{d}]` | `list_schemas` |
| resumable upload load job | `POST /upload/bigquery/v2/projects/{p}/jobs?uploadType=resumable` + `PUT` | seeds (CSV, explicit schema, `WRITE_TRUNCATE`) |

dbt also emits DDL and DML around compiled SELECTs, which the dialect handles as statements:
`CREATE SCHEMA IF NOT EXISTS`, `CREATE OR REPLACE TABLE ... PARTITION BY ... CLUSTER BY ...
OPTIONS(...) AS`, `CREATE OR REPLACE VIEW`, `MERGE ... ON (FALSE)` and keyed `MERGE`,
`ALTER TABLE ... ADD COLUMN`, `ALTER TABLE ... SET OPTIONS`, and a leading JSON query comment with
job labels. Partitioning, clustering and options are recorded as object properties, not executed.

Client configuration (endpoint override, dummy credentials, result formats) is documented with
source citations in [`client-compat.md`](client-compat.md).

## Testing

| Suite | What | Where |
|---|---|---|
| unit | every transform, shim, type mapping, command handler, engine behaviour | `tests/unit`, every PR, Python 3.11 to 3.13 |
| corpus | differential golden cases (ADR 0013) | `corpus/`, `tests/corpus`, every PR |
| clients | official unmodified clients, oldest and latest, against the Docker image | `tests/clients`, CI `clients` job |
| dbt | `dbt seed/run/test` on `examples/` with the standard adapters, oldest and latest | `tests/dbt`, CI `dbt` job |

`docs/compatibility.md` is generated from corpus and client results, never written by hand.
Coverage is reported as "runs and has no known divergence".

## Changes from the original brief after Phase 0

- **Translation fixes are per source dialect** (ADR 0008). The brief implied a single SQLGlot-based
  translation; Phase 0 showed SQLGlot mistranslates silently, and the fixes differ per warehouse.
- **Translation is schema-aware** (ADR 0010), so the `Dialect.plan` signature carries the live
  catalog.
- **Runtime error parity** (ADR 0011) is new: Crucible raises wherever BigQuery raises.
- **Timezone** (ADR 0012): `lock_configuration` stops per-cursor `SET TimeZone`, so DuckDB is pinned
  to UTC and session timezones are applied in translation.
- **The BigQuery API subset is larger than queries alone** (ADR 0004, table above): client-minted job
  ids with 409 handling, anonymous result tables, and resumable uploads for seeds.
- **Credentials are not a blocker for dbt-bigquery**: `method: oauth-secrets` with a dummy token works
  offline on 1.9 and 1.12, with `api_endpoint` (1.12) or `BIGQUERY_EMULATOR_HOST` (1.9)
  ([client-compat.md](client-compat.md)).
- **Docker host binding**: the image sets `CRUCIBLE_HOST=0.0.0.0` so port publishing works; the CLI
  default stays `127.0.0.1` (ADR 0006).
- **`pytz` is a core dependency**: DuckDB needs it to return TIMESTAMP WITH TIME ZONE values to Python.
