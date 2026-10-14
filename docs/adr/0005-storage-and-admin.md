# 5. Storage and admin routes

Status: accepted

## Decision

- In memory by default, which suits CI.
- `--database PATH` persists state: `PATH` holds Crucible's metadata (catalog list, object
  properties) and each catalog is a file in `PATH.catalogs/`. Restarting with the same `PATH`
  re-attaches every catalog. Only `PATH.catalogs/` is reachable from SQL (ADR 0006).
- `POST /_crucible/reset` drops every catalog, all metadata, sessions and jobs.
- `GET /_crucible/health` returns status, storage, catalog count, DuckDB and SQLGlot versions,
  and the dialect.
- Admin routes live under `/_crucible/` so they cannot collide with warehouse API paths.

## Consequences

- Job history is not persisted, only data and metadata.
