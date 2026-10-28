# 16. Iceberg external tables

Status: proposed

## Context

A dbt project that ingests into Apache Iceberg tables reads them from BigQuery through Iceberg
external tables:

```sql
CREATE OR REPLACE EXTERNAL TABLE ds.t
WITH CONNECTION `project.us.connection`
OPTIONS (format = 'ICEBERG', uris = ['gs://bucket/t/metadata/v3.metadata.json'])
```

BigQuery reads the snapshot that the named metadata file points to. It does not find newer
metadata by itself: "you must manually update the URI of the JSON metadata file to keep the
Iceberg external table up to date"
([Iceberg external tables](https://cloud.google.com/bigquery/docs/iceberg-external-tables)). So a
pipeline recreates the table after each ingestion. Locally, the same project writes its Iceberg
tables to disk and sends `file://` URIs, which BigQuery itself would not accept for this table.
Crucible refuses the statement today: SQLGlot 30.22.0 does not parse `WITH CONNECTION`, so it
falls back to a generic command.

ADR 0006 is the constraint here. DuckDB starts with extension autoinstall and autoload off,
`allowed_directories` is set to the catalog directory, `enable_external_access` is false, and the
configuration is locked. Load jobs never read file paths in SQL (ADR 0015 kept that rule). An
external table is different: its data is files, by definition.

Every claim below marked *checked* was run against DuckDB 1.5.6 with the engine's lockdown
applied in its order. The Iceberg tables were written by pyiceberg (SqlCatalog, local warehouse):
a create, two appends, an added column, then an append whose file does not write an existing
column, an overwrite, and an expire of old snapshots.

## Decision

### Reader: DuckDB's `iceberg` extension

Use DuckDB's `iceberg` extension, loaded before `lock_configuration`, and never installed at run
time.

- `iceberg_scan('<path>/metadata/<n>.metadata.json')` reads the snapshot that file names, through
  its manifest list and manifests. *Checked*: each of the six metadata versions returned exactly
  its own snapshot's rows, including the pre-overwrite versions after the overwrite and expiry.
- Schema evolution follows field IDs, not names. *Checked*: an added column reads as NULL in
  older files, and a column still in the schema that a newer file does not write reads as NULL
  in that file.
- `iceberg_metadata()` lists the data and delete files of a snapshot from its manifests, so
  Crucible can inspect a table before accepting it.
- The extension needs `avro`, which it loads itself from the same extension directory. *Checked*:
  this works with autoload off.
- A Crucible-owned reader would have to parse Avro manifest lists, manifests, partition specs,
  schema evolution and delete files. That is a large native-format component with no
  test oracle other than the extension itself, so it is rejected.

**Pinning.** Extensions are built per DuckDB version and platform. The image installs `iceberg`
and `avro` for DuckDB 1.5.6 at build time, into a fixed `extension_directory`, from DuckDB's
signed repository. It checks each file's SHA-256 against hashes committed in the repository (one
per platform: `linux_amd64`, `linux_arm64`). The repository can rebuild an extension for the same
DuckDB version, so without the hash check the image would not be reproducible.
`allow_unsigned_extensions` stays false, so DuckDB also verifies the signature on `LOAD`. The
build checked here reported `iceberg 890b78a9c` and `avro f9d5902`.

**Loading.** The engine runs `LOAD iceberg` only when an external data directory is configured
(below), before `lock_configuration`. After the lock, *checked*: `INSTALL` fails and settings
cannot change; `LOAD` of the already-loaded extension is a no-op. Outside Docker, Crucible never
downloads anything. If the extension is not installed, `--external-data` fails at startup with
the command that installs it. Without `--external-data`, the engine is exactly as it is today.

### URI scheme: `file://` under the external data directory only

- Accepted: exactly one URI, `file:///<absolute path>` ending in `.metadata.json`, whose real path
  (symlinks resolved) is inside the external data directory. This is an emulator-only extension
  and is documented as one.
- Refused, with `Not supported by Crucible`: `gs://`, `s3://`, `azure://` and any other scheme,
  relative paths, paths outside the directory, more than one URI, and a URI that is not a
  metadata file.
- Mapping `gs://bucket/...` to a local directory is rejected. An Iceberg table's metadata,
  manifests and data file entries hold absolute paths, so a table written to GCS names `gs://`
  files all the way down. Rewriting the top URI would not change that, and DuckDB cannot rewrite
  paths inside manifests. Reading GCS directly would need `httpfs` and credentials, which ADR 0006
  rules out.
- Absolute paths inside the metadata are why the writer and Crucible must see the same paths.
  Inside Docker, mount the warehouse at the path the writer used (for example
  `-v ./data:/app/data:ro` with `--external-data /app/data`).
- `tables.get` reports `sourceUris` exactly as written. The rest of `externalDataConfiguration`
  is BigQuery's recorded shape (below).

### File access: one read-only directory

- `crucible serve --external-data DIR` (or `CRUCIBLE_EXTERNAL_DATA`) adds `DIR/` to
  `allowed_directories`, next to the catalog directory. No other path is added. In-memory mode
  then allows that one directory too.
- *Checked*: `iceberg_scan` and `read_csv` outside the allowed directories raise
  `PermissionException`. A metadata file inside the directory whose manifests or data files lie
  outside it also fails, at the first file outside.
- DuckDB has no read-only flavour of `allowed_directories`, and *checked*: SQL can still reach
  files in an allowed directory with DuckDB's own file functions. Crucible's guarantee is that
  no SQL it generates names a path except the validated metadata file in a view body (below).
  `EXPORT DATA` and `COPY` are refused already (*checked*). The documented setup also mounts the
  directory read-only, so the OS enforces it.
- Gap found while checking: a DuckDB table function in FROM reaches DuckDB today.
  `SELECT * FROM read_parquet('/tmp/x.parquet')` is stopped only by the lockdown's
  `PermissionException`; `iceberg_scan` as a scalar call is refused. Once a directory is allowed,
  that would let client SQL read any file in it. The build refuses every table function in FROM
  that is not a BigQuery one (UNNEST, and the INFORMATION_SCHEMA views). That fix lands with the
  build, and a test reads a file in the external directory through `read_parquet` and expects
  `Not supported by Crucible`.
- ADR 0006 gets an amendment: "only the catalog directory, and the configured external data
  directory for reads, are reachable from SQL".
- DuckDB's external file cache (`enable_external_file_cache`, on by default) is turned off
  before the lock. Iceberg files are immutable, so the cache cannot give stale rows. But it can
  keep answering for a deleted file, where BigQuery would fail. *Checked*: a deleted data file
  still read through the cache.

### DDL

- `CREATE [OR REPLACE] EXTERNAL TABLE [IF NOT EXISTS] <name> WITH CONNECTION <connection>
  OPTIONS (format = 'ICEBERG', uris = [...])`, with `<connection>` a quoted name or `DEFAULT`.
  Crucible parses this statement itself, because SQLGlot does not (Context). Other options and
  other formats are refused until recorded.
- The connection is stored as metadata only. It is never resolved, and no credential is read.
- Crucible stores the table as a DuckDB view,
  `SELECT <cast per column> FROM iceberg_scan('<resolved path>')`, with the column list and casts
  fixed at creation. It also stores properties `{external: {format, uris, connection}}`. Queries
  then resolve it like any table, through the existing qualify and annotate steps (ADR 0010).
- The schema comes from the metadata file's current schema at creation, as BigQuery autodetects
  it. Because a metadata file is immutable, that schema never goes stale. Repointing is
  `CREATE OR REPLACE` with the newer metadata file, as with BigQuery.
- `DROP [EXTERNAL] TABLE` removes the view and its properties and never touches files.
- DML, `ALTER TABLE` and `tables.patch` on an external table, and `FOR SYSTEM_TIME AS OF`, are
  refused until BigQuery's errors are recorded. BigQuery documents time travel over retained
  Iceberg snapshots but not for external tables generally, so it is left unsupported.

### Snapshot semantics

- The view names one metadata file, so each query reads that file's current snapshot: no newer
  one, and no older one. Rows come only from the files its manifests list, never from a glob or
  directory listing. A data file left on disk by an overwrite or an expiry is never read.
- At creation, Crucible refuses a snapshot it cannot read exactly. That covers a snapshot with
  delete files (position or equality deletes, or v3 deletion vectors: `iceberg_metadata` content
  `DELETE`), and data files that are not Parquet (BigQuery reads only Parquet). BigQuery applies
  deletes (merge-on-read). Crucible accepts them once a recording confirms that DuckDB applies
  them identically. The snapshot cannot change after creation, so checking once is enough.
- A missing data or metadata file at query time is a runtime error. Its message is recorded
  before Crucible copies it; until then it is `Not supported by Crucible` plus the file name.

### Types

Each Iceberg type maps as BigQuery documents
([data mapping](https://cloud.google.com/bigquery/docs/iceberg-external-tables#data_mapping)).
Each view column is cast to Crucible's storage type for that BigQuery type (ADR 0014):

| Iceberg | BigQuery | Crucible |
|---|---|---|
| boolean | BOOL | as stored |
| int, long | INT64 | cast to BIGINT |
| float, double | FLOAT64 | cast to DOUBLE (float32 widens exactly) |
| decimal(P, S) | NUMERIC or BIGNUMERIC by precision | NUMERIC when S ≤ 9 and P − S ≤ 29; otherwise refused, as BIGNUMERIC is today |
| date | DATE | as stored |
| time | TIME | as stored (microseconds) |
| timestamp | DATETIME | DuckDB TIMESTAMP |
| timestamptz | TIMESTAMP | DuckDB TIMESTAMPTZ |
| string | STRING | as stored |
| binary, fixed(L) | BYTES | BLOB |
| uuid | BYTES | the 16 bytes, from DuckDB's UUID |
| list\<T\> | ARRAY\<T\> | element-wise; a NULL element is refused until recorded (BigQuery arrays cannot hold NULL) |
| struct | STRUCT | field-wise |
| map\<K, V\> | ARRAY\<STRUCT\<key K, value V\>\> | `map_entries` |

The Iceberg v3 types BigQuery does not support are refused at creation: nanosecond timestamps,
`unknown`, `variant`, `geometry` and `geography`. So are default values.

`tables.get` returns `type: EXTERNAL`, the mapped schema and `externalDataConfiguration`. Which
fields it carries, and whether `numRows` and `numBytes` appear, is copied from the recording.
`INFORMATION_SCHEMA.TABLES` lists the table with `table_type = 'EXTERNAL'`.

### Tests

pyiceberg (with its pyarrow and SQLite extras) becomes a dev-only dependency. Tests build
deterministic Iceberg tables in temporary directories; committed fixture tables would not be
portable, because their metadata holds absolute paths. The synthetic acceptance cases are:

- append;
- recreate and repoint with `CREATE OR REPLACE`;
- expire snapshots;
- add a column;
- a column that stays in the schema but newer files no longer write.

Each checks that a query sees the named snapshot's schema and live rows, with no stale or
duplicate files. CI installs the extension the way the image does, and runs these tests with
`CRUCIBLE_REQUIRE_ICEBERG=1`, so a missing extension fails CI instead of skipping the tests. A
local run without the extension skips them.

## Recording

A real recording needs a GCS bucket and a BigQuery connection in the recording project:

1. Enable the BigQuery Connection API.
2. Create a Standard bucket in the dataset location, with a lifecycle rule that deletes objects
   after 1 day.
3. Create a `CLOUD_RESOURCE` connection in that location.
4. Grant the connection's service account `roles/storage.objectViewer` on the bucket only.

A new `record-api` step (maintainer-only, as today) writes the acceptance tables with pyiceberg
to `gs://<bucket>/<run id>/`, using the maintainer's credentials and pyarrow's GCS filesystem. It
then:

- runs each `CREATE EXTERNAL TABLE`, `tables.get`, the queries, and the refused statements
  (DML, deletes, a missing file);
- records the responses, redacted as usual, including the bucket name;
- drops the tables and deletes the prefix.

Replay maps a recorded `gs://` URI to the same table built locally under `file://`. A listed
difference covers `sourceUris`.

The cost of one run is under US$0.01:

- About 40 queries at BigQuery's 10 MB minimum per referenced table, about 400 MB at on-demand
  pricing, roughly US$0.003.
- Under 1 MB stored for a day.
- A few hundred GCS operations, under US$0.002.
- The connection itself is free.

## Consequences

- The image grows by the two extension files (about 40 MB on macOS arm64; Linux sizes still to
  be measured) and gains a build-time download from DuckDB's repository, checked by hash.
- ADR 0006 is amended: one more read-only directory is reachable when configured, and one
  extension is loaded.
- Iceberg reads follow the extension's version, and a DuckDB upgrade (ADR 0008) must re-check
  them, as it does SQLGlot output today.
- `pip install crucible-emulator` alone gets no Iceberg support until the user installs the
  extension. The Docker image has it.

## Open questions

- Whether BigQuery accepts a metadata file whose snapshot has delete files, and whether DuckDB
  applies them identically (to record).
- The exact `tables.get` shape, the error messages for DML, a missing file, and an unsupported
  type (to record).
- Whether BigQuery maps a decimal with P − S ≤ 29 but S > 9 to BIGNUMERIC, as the precision rule
  suggests (to record).
