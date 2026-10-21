# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project does not yet follow semantic versioning.

## [Unreleased]

### Added

- `dryRun` on `jobs.insert` and `jobs.query`: the statement is planned, not run, and the response carries
  its schema and `statementType`; `totalBytesProcessed` is `"0"` (byte estimates are not emulated).
- `docs/bigquery-features.md`: supported and unsupported BigQuery features, each linked to its test.
- README roadmap.
- `INFORMATION_SCHEMA.COLUMNS`, `COLUMN_FIELD_PATHS`, `TABLE_OPTIONS`, `MATERIALIZED_VIEWS` and `__TABLES__`, with
  the columns `dbt docs generate` reads; the CI dbt suite now runs `dbt docs generate`.
- Corpus cases (to be recorded) for 62 common features that had no test.
- `tabledata.insertAll` (`insert_rows`, `insert_rows_json`) with per-row `insertErrors`, `skipInvalidRows` and
  `ignoreUnknownValues`. UPDATE and DELETE over streamed rows give BigQuery's streaming-buffer error.
- `UPDATE` (including `UPDATE ... FROM`), `DELETE` and `TRUNCATE TABLE`, with `numDmlAffectedRows` and
  `dmlStats`. UPDATE and DELETE require a WHERE clause, as in BigQuery.
- Table and column metadata that dbt `persist_docs` and `labels` send: table `description` and
  `labels` (`tables.insert`, `tables.patch`, `OPTIONS`, `ALTER ... SET OPTIONS`), column descriptions
  (`ALTER COLUMN ... SET OPTIONS`, and `tables.patch`), `REQUIRED` columns (NOT NULL), and `ALTER VIEW
  ... SET OPTIONS`. `tables.get` returns them. Other field options stay refused.

### Fixed

- Reconciled with the Milestone 1 recording: UPDATE/DELETE without WHERE report `at [line:col]`;
  TRUNCATE TABLE reports the rows it removed and its destination table; getQueryResults of UPDATE,
  DELETE and TRUNCATE carries the table's schema; CREATE TABLE and ALTER TABLE jobs report their
  destination (and ALTER its `ddlTargetTable`); insertAll lists invalid rows before stopped ones;
  streamed rows are left out of `numRows` and `__TABLES__.row_count`; the streaming-buffer and NOT NULL
  errors use BigQuery's wording; ARRAY columns are not nullable in `INFORMATION_SCHEMA.COLUMNS`;
  partitioned tables list `require_partition_filter`.
- `BOOL_AND` and `BOOL_OR` are not BigQuery functions and now fail with `Function not found`.
- The `region-<location>` INFORMATION_SCHEMA form is refused until it can be recorded, and
  TABLE_OPTIONS is refused in a dataset with a default table expiration.
- NaN follows BigQuery's rules instead of DuckDB's: comparisons with NaN are false (`!=` true), NaN sorts
  between NULL and -inf in every ORDER BY, and MIN, LEAST and GREATEST return NaN. Comparisons Crucible
  cannot prove for FLOAT64 (IN subqueries, IS DISTINCT FROM, NULLIF, simple CASE, RANGE-offset frames)
  are refused.
- `LIKE` now treats a backslash as BigQuery's escape character (`'a%' LIKE 'a\\%'` was false).
- `EXTRACT(WEEK(<weekday>))` is supported instead of refused.
- `MERGE ... USING s` with an unaliased source table now resolves `s.column`.
- `CREATE MATERIALIZED VIEW` was accepted and created nothing; it now raises `Not supported by Crucible`.
- Functions SQLGlot does not model (such as `RANGE`), BigQuery ML functions and `SHA512` raise
  `Not supported by Crucible` instead of running DuckDB's namesake or leaking a DuckDB error.

## [0.1.0a2] - 2026-10-20

### Added

- Multipart uploads (`uploadType=multipart`) for CSV load jobs, used by dbt Fusion for seeds.
- CSV loads accept the BOOL spellings `True` and `False`, as written by Python tools.
- `examples/dbt-bigquery`: an original dbt project with seeds, views, tables, data tests and unit tests.
- README sections on supported dbt versions, current limits and alternatives.
- Issue templates for bug reports and unsupported features.

### Changed

- The Python client examples moved to `examples/python-bigquery`.
- The project is described as a BigQuery emulator, with Snowflake planned.

### Fixed

- `crucible corpus record-api` now removes stale fixtures with three-digit numbers.
- Time travel (`FOR SYSTEM_TIME AS OF`) and functions DuckDB lacks (such as `ST_*`) raise
  `Not supported by Crucible` instead of a DuckDB parser or catalog error.

## [0.1.0a1] - 2026-10-17

### Added

- Initial project scaffolding.
- BigQuery translation backbone: schema-aware translation, output types from the BigQuery AST,
  BigQuery literal typing, NUMERIC as DECIMAL(38,9), CREATE TABLE and INSERT with BigQuery coercion.
- BigQuery error parity, SAFE., function fixes, UNNEST, ARRAY_AGG and the statements dbt emits.
- `crucible corpus record-api`: redacted wire-format fixtures from the real BigQuery REST API.
