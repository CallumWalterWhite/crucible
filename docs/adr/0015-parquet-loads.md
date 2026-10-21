# 15. Parquet loads

Status: proposed

## Context

A load job with `sourceFormat: PARQUET` is refused today (`docs/bigquery-features.md`). To accept
one, the file's columns must become DuckDB rows, and the way the bytes reach DuckDB decides what
SQL can reach. ADR 0006 is the constraint: DuckDB starts with extensions off and
`enable_external_access = false`, `allowed_directories` is the catalog directory, and the
configuration is locked (`src/crucible/engine/engine.py`, lines 46-53). Its decision also says load
jobs "ingest data through the API in Python, never through file paths in SQL".

Two ways to get Parquet rows into DuckDB were considered. Both were checked against DuckDB 1.5.6
(the pinned version) with the engine's settings applied: the lockdown set in that order, then a
scratch directory standing in for the catalog directory.

## Options

**(a) Temp file and `read_parquet`.** Write the upload's bytes to a file in a staging directory,
then `read_parquet('<path>')` in SQL.

- Works under the lockdown, but only for paths inside `allowed_directories`. The check was run:
  `read_parquet` of a file inside the directory returned rows; the same call on a file outside it
  raised `PermissionException`.
- The staging directory must be allowed at startup, because `allowed_directories` cannot change
  after `lock_configuration`. An in-memory engine has no allowed directory today, so it would gain
  one, and the rule "only the catalog directory is reachable from SQL" would become "the catalog
  directory and a staging directory".
- It puts client bytes on disk and needs a size bound, unique names, cleanup after failures, and a
  sweep for files left by a crash.
- It breaks ADR 0006's rule: the load reads a file path in SQL.
- DuckDB's reader decides the column types from the file, not from the load schema. Checking
  them against the schema would be a second pass over the file's metadata, and the rules for a
  mismatch would be DuckDB's, not BigQuery's (not tested).

**(b) In-process reader in Python.** Parse the bytes in Python, check each column against the load
schema, and hand DuckDB an Arrow table, with no file path in SQL.

- DuckDB cannot read Parquet from a Python object by itself. `read_parquet` takes a path or a URL.
  `read_blob` returns bytes but does not parse them.
- An fsspec in-memory filesystem registered with DuckDB (`register_filesystem`, then
  `read_parquet('memory:///x.parquet')`) reads the bytes with no disk write. Under the lockdown it
  fails: `PermissionException: file system operations are disabled by configuration`. So this
  route needs ADR 0006 to change, and it is not recommended.
- A Parquet parser in Python would be a large, untested component. It is not recommended.
- pyarrow parses the bytes in-process, `pq.read_table(pa.BufferReader(data))`, and
  `connection.register(name, table)` makes the table visible to SQL. Under the lockdown this worked
  (26.0.0 with DuckDB 1.5.6), and no file path appears in SQL. Each column can then be checked
  against the schema before anything is inserted.

## Recommendation

Option (b), with pyarrow: parse in Python, reject any column whose Arrow type does not map exactly
to the load schema's type, and insert from the registered table. It keeps ADR 0006 as written, and
it keeps type decisions in Crucible's code rather than in DuckDB's reader.

This is a design note. Nothing is built. A later change would add pyarrow as a server dependency
and pin it exactly, as ADR 0008 does for sqlglot and DuckDB. It would also need a byte limit on
the upload before parsing, since pyarrow reads the whole file.

## Consequences

- A new native dependency enters the server extra. The Docker image and every supported Python
  (3.11 to 3.13) need pyarrow wheels, which is not yet checked.
- The parser handles untrusted bytes in native code. Option (a) has the same exposure, since
  DuckDB's reader is also native, but (b) adds no new file-system surface.
- Parquet's type rules need their own recorded cases, as NDJSON's do (`ndjson_*` in
  `record-api`). Logical types such as DECIMAL, the TIMESTAMP units, and INT96 timestamps are
  unsupported until a recording confirms each one.

## Open questions

- Which Arrow types load into which schema types, and how BigQuery treats a mismatch: refused,
  or cast. Only a recording can answer this.
- The upload size limit before parsing, and whether the limit should match the CSV and NDJSON
  paths, which read the whole body into memory.
- Whether to support compression codecs beyond those pyarrow ships with.
