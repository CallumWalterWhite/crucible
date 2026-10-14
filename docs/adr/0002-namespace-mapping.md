# 2. Namespace mapping onto DuckDB catalogs and schemas

Status: accepted

## Context

Both warehouses have three-part names. DuckDB has catalogs (ATTACHed databases), schemas and
tables. DuckDB matches identifiers case-insensitively, and the warehouses do not always.

## Decision

- BigQuery: project → catalog, dataset → schema, table → table.
- Snowflake: database → catalog, schema → schema, table → table.
- Each catalog is an `ATTACH`ed database: in memory by default, or a file under the storage
  directory (ADR 0005). The engine passes names through exactly; `engine/catalog.py` quotes every
  identifier.
- Identifier case rules live in `dialects/<d>/metadata.py`:
  - BigQuery dataset and table names are case-sensitive; column names are not. Lookups compare the
    stored name exactly, so `ds.T` does not find `ds.t`. Creating a dataset or table whose name
    differs only by case from an existing one raises `CrucibleUnsupportedError`, because DuckDB
    cannot hold both.
  - Snowflake folds unquoted identifiers to upper case and keeps quoted identifiers as written.
    Two quoted identifiers that differ only by case raise `CrucibleUnsupportedError`.
- Reserved names: DuckDB's `memory`, `system`, `temp` and Crucible's metadata catalog
  `__crucible__` cannot be warehouse catalogs. Every DuckDB catalog has an internal `main` schema,
  so a warehouse schema named `main` (any case) raises `CrucibleUnsupportedError`.
- Metadata DuckDB cannot hold (partitioning, clustering, labels, descriptions, expiration,
  warehouse-only type details) is stored as JSON properties per object in
  `__crucible__.main.objects`. Dialects replace an object's properties on every CREATE and clear
  them on DROP.

## Consequences

- Hyphenated BigQuery project ids work as catalog names.
- Case-only collisions are rare and fail loudly instead of corrupting a table.
