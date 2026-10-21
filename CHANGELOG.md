# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project does not yet follow semantic versioning.

## [Unreleased]

### Added

- `docs/bigquery-features.md`: supported and unsupported BigQuery features, each linked to its test.
- README roadmap.
- Corpus cases (to be recorded) for 62 common features that had no test.
- `UPDATE` (including `UPDATE ... FROM`), `DELETE` and `TRUNCATE TABLE`, with `numDmlAffectedRows` and
  `dmlStats`. UPDATE and DELETE require a WHERE clause, as in BigQuery.

### Fixed

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
