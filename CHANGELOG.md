# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project does not yet follow semantic versioning.

## [Unreleased]

### Fixed

- A function call in FROM is refused unless it is UNNEST: `read_parquet` and `read_csv` reached
  DuckDB, where only the file-access lockdown stopped them, and `generate_series` ran.
- CAST and SAFE_CAST of NUMERIC to STRING, and a NUMERIC argument of CONCAT or `||`, write no
  trailing zeros, as BigQuery does: `CAST(NUMERIC '4.10' AS STRING)` gave `'4.100000000'`. A
  NUMERIC argument of another string function (LENGTH, UPPER, LIKE, STRING_AGG,
  ARRAY_TO_STRING, ...) and a NUMERIC value for a STRING variable are refused; DuckDB converted
  them silently.
- NUMERIC values with more than 28 significant digits were rounded in query results (Python's
  default decimal precision): `NUMERIC '12345678901234567890.123456789'` came back as
  `12345678901234567890.12345679`. The largest NUMERIC was also refused as a literal, as a
  string cast to NUMERIC, and in insertAll.
- `TO_JSON_STRING(ROUND(NUMERIC '40.4'))` gave `4`: zeros were trimmed from DuckDB's scale-0
  text of the rounded value.
- `ARRAY_CONCAT` returns NULL when an input array is NULL, as BigQuery documents;
  `ARRAY_CONCAT([1], NULL)` gave `[1]`.
- UNNEST and `ARRAY(SELECT ...)` over an array built with `||` keep its element type:
  `TO_JSON_STRING(ARRAY(SELECT x FROM UNNEST([1, 2] || [3]) AS x))` was refused as "TO_JSON_STRING
  of a UNKNOWN value", and `SELECT x FROM UNNEST([1, 2] || [3] || [4])` failed.
- REVERSE, ENDS_WITH, STARTS_WITH, BIT_COUNT and STRING_AGG with a delimiter work on BYTES
  byte for byte. REVERSE and STARTS_WITH were refused; the others raised DuckDB errors.
- `BIT_COUNT` of a negative INT64 literal counts 64 bits: `BIT_COUNT(-1)` gave 32.
- STRPOS, INSTR, LPAD and RPAD of BYTES are refused: they matched hex digits across bytes or
  padded with the whole pattern, so `STRPOS(b'\x12\x34', b'\x23')` gave 2 and
  `LPAD(b'a', 4, b'xy')` gave 7 bytes.
- LIKE, SUBSTR, SPLIT, the REGEXP functions, CHAR_LENGTH and ARRAY_TO_STRING of BYTES raise
  `Not supported by Crucible` instead of a DuckDB binder error.

## [0.1.0a4] - 2026-10-28

### Added

- `tables.insert` with `timePartitioning`, `rangePartitioning` and `clustering` (`create_table` with a
  partitioned or clustered `Table`), returned by `tables.get`. Partition expiry, ingestion-time
  partitioning and `requirePartitionFilter: true` are refused.
- `PARTITION BY DATE(<timestamp or datetime column>)`.
- Query jobs with a `destinationTable` (`QueryJobConfig(destination=...)`), with `WRITE_EMPTY`,
  `WRITE_TRUNCATE`, `WRITE_APPEND`, `CREATE_NEVER` and query parameters. A failed job leaves the
  destination untouched.
- Parquet load jobs (`sourceFormat=PARQUET`, `load_table_from_file`) with an explicit schema, by
  resumable or multipart upload (ADR 0015, now accepted): INT64, FLOAT64, BOOL, DATE, TIMESTAMP and
  STRING, NULLs and REQUIRED fields; a failed load appends nothing. pyarrow joins the `server` extra.
- `FARM_FINGERPRINT`: FarmHash Fingerprint64 as BigQuery's signed INT64, for STRING (UTF-8 bytes)
  and BYTES, in pure Python. It was refused before.

### Fixed

- PARTITION BY and CLUSTER BY check column types as BigQuery does: a STRING partitioning column or a
  FLOAT64 clustering column was accepted.
- CAST and SAFE_CAST between STRING and BYTES encode and decode UTF-8, as BigQuery does. They
  followed DuckDB's escaped text form: `CAST('\\x41' AS BYTES)` gave one byte, `CAST(b'\xc3\xa9' AS
  STRING)` gave the text `\xC3\xA9`, and `CAST('é' AS BYTES)` failed. Invalid UTF-8 to STRING is
  BigQuery's `Invalid cast of bytes to UTF8 string` (SAFE_CAST gives NULL).
- `BYTES || BYTES` is BYTES; it was STRING in escaped text form.
- A STRING and a BYTES operand of one function or operator (CONCAT, `||`, comparisons, IN,
  COALESCE, IF, CASE, ...) and a variable DEFAULT or SET of the other type are errors; DuckDB
  silently cast one to the other. CONCAT, `STRING = BYTES` and a STRING literal for a BYTES
  variable give BigQuery's recorded errors; the other mixes are refused until recorded.
- LOWER, UPPER, REVERSE, REPLACE, STARTS_WITH, TRIM, LTRIM, RTRIM and STRING() of BYTES are refused:
  they ran on DuckDB's escaped text form and could give wrong bytes.
- TO_JSON_STRING of a bare BYTES, TIMESTAMP or TIME value gives a JSON string; it raised a DuckDB
  error.
- NULLIF over FLOAT64 follows IEEE-754 (NaN equals nothing), so `NULLIF(NaN, NaN)` is NaN. It was
  refused.

- Reconciled with the Part 2 recording:
  - `tables.insert` partitioning and clustering errors are HTTP 400 with reason `invalid` and
    BigQuery's messages, including HOUR partitioning on a DATE field and a repeated clustering
    field; DDL clustering on FLOAT64 and a bad PARTITION BY use BigQuery's messages too;
  - `getQueryResults` of a query job with a destination returns the destination's rows (the
    whole table after WRITE_APPEND); a missing CREATE_NEVER destination names no location;
    `tables.get` of a query destination lists `mode: NULLABLE`;
  - Parquet loads accept int32 columns, zone-less timestamps (as UTC), a NULLABLE field missing
    from the file (NULL) and a file column not in the schema (ignored); NULL in a REQUIRED field
    and an unreadable file fail with BigQuery's errors;
  - NET.REG_DOMAIN ignores labels before the result (`a..b.com` gives `b.com`, a leading dot or
    an underscore there is fine) and keeps one trailing dot (`example.com.`).

## [0.1.0a3] - 2026-10-28

### Added

- `NET.REG_DOMAIN`, using the ICANN section of a vendored, pinned Public Suffix List (MPL-2.0, in
  `NOTICE`). Hosts whose answer BigQuery does not document are refused. numpy is now a dependency:
  DuckDB needs it to run Python functions.
- SQL user-defined functions: `CREATE TEMP FUNCTION`, `CREATE [OR REPLACE] FUNCTION`, `DROP FUNCTION`, typed
  arguments and RETURNS with BigQuery's `No matching signature` error, and persistent functions in
  `routines.list` and `routines.get`. JavaScript, remote, table-valued and ANY TYPE functions are refused.
- BigQuery scripting subset for dbt: `DECLARE` and `SET` with script variables, so dbt-bigquery's dynamic
  `insert_overwrite` and `microbatch` work. Copy jobs (`client.copy_table`) with `$partition` decorators,
  so `copy_partitions` works. The CI dbt project now runs all three strategies twice.
- Query parameters (`queryParameters`, named and positional, including ARRAY and STRUCT) on `jobs.query`
  and `jobs.insert`, bound as DuckDB parameters.
- `examples/ci/github-actions.yml`: a GitHub Actions recipe that runs the dbt example against Crucible as a service
  container. CI runs the same dbt steps (`examples/ci/run-corner-cafe.sh`) against the image built from the PR.
- `dryRun` on `jobs.insert` and `jobs.query`: the statement is planned, not run, and the response carries
  its schema and `statementType`; `totalBytesProcessed` is `"0"` (byte estimates are not emulated).
- `docs/bigquery-features.md`: supported and unsupported BigQuery features, each linked to its test.
- README roadmap.
- `INFORMATION_SCHEMA.COLUMNS`, `COLUMN_FIELD_PATHS`, `TABLE_OPTIONS`, `MATERIALIZED_VIEWS` and `__TABLES__`, with
  the columns `dbt docs generate` reads; the CI dbt suite now runs `dbt docs generate`.
- `crucible mirror-schema`: copies table and view schemas (no data) from a BigQuery project into Crucible,
  reading only datasets.get, tables.list and tables.get.
- Corpus cases (to be recorded) for 62 common features that had no test.
- `tabledata.insertAll` (`insert_rows`, `insert_rows_json`) with per-row `insertErrors`, `skipInvalidRows` and
  `ignoreUnknownValues`. UPDATE and DELETE over streamed rows give BigQuery's streaming-buffer error.
- `UPDATE` (including `UPDATE ... FROM`), `DELETE` and `TRUNCATE TABLE`, with `numDmlAffectedRows` and
  `dmlStats`. UPDATE and DELETE require a WHERE clause, as in BigQuery.
- Table and column metadata that dbt `persist_docs` and `labels` send: table `description` and
  `labels` (`tables.insert`, `tables.patch`, `OPTIONS`, `ALTER ... SET OPTIONS`), column descriptions
  (`ALTER COLUMN ... SET OPTIONS`, and `tables.patch`), `REQUIRED` columns (NOT NULL), and `ALTER VIEW
  ... SET OPTIONS`. `tables.get` returns them. Other field options stay refused.
- Load jobs with `sourceFormat=NEWLINE_DELIMITED_JSON` (`load_table_from_json`) and an explicit schema, with
  `ignoreUnknownValues`, nested `RECORD` and `REPEATED` fields, and `WRITE_TRUNCATE` or `WRITE_APPEND`. Values that
  BigQuery would reject are refused as unsupported until its message is recorded. Parquet loads are a design
  note only (ADR 0015, proposed).

### Fixed

- A named STRUCT built from a correlated column in an array passed to UNNEST failed with
  `Binder Error: Need named argument for struct pack` when a field called NULLIF or another function
  DuckDB implements as a macro. Such STRUCTs are now built as typed ROWs.
- An aliased UNNEST of a literal STRUCT array, read through `v.field`, failed with `Referenced table
  not found`.
- Reconciled with the Milestone 1 recording: UPDATE/DELETE without WHERE report `at [line:col]`;
  TRUNCATE TABLE reports the rows it removed and its destination table; getQueryResults of UPDATE,
  DELETE and TRUNCATE carries the table's schema; CREATE TABLE and ALTER TABLE jobs report their
  destination (and ALTER its `ddlTargetTable`); insertAll lists invalid rows before stopped ones;
  streamed rows are left out of `numRows` and `__TABLES__.row_count`; the streaming-buffer and NOT NULL
  errors use BigQuery's wording; ARRAY columns are not nullable in `INFORMATION_SCHEMA.COLUMNS`;
  partitioned tables list `require_partition_filter`.
- Reconciled with the Milestone 2 recording:
  - a persistent function must be called with its dataset; the bare name is `Function not found`;
  - `No matching signature` uses BigQuery's multi-line message and location;
  - `Variable 'x' redeclaration`, `Undeclared variable` and `Query parameter 'x' not found` are
    located where BigQuery reports them;
  - a temporary function plus one query is that query's statement type, not `SCRIPT`;
  - script jobs report `numChildJobs` and their schema; copy jobs report `statistics.copy`;
    `CREATE FUNCTION` jobs report `ddlTargetRoutine`; `routines.get` returns the body as written;
  - dry runs have no job id, report the destination table, and fail as HTTP errors;
  - NDJSON loads give BigQuery's errors for an unknown field and a non-integer string in a one-row
    file, and load a string into a JSON column as a JSON string.
- The fixture redaction guards also reject URL-encoded email addresses (`%40`).
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
