# Crucible

Crucible is a local BigQuery API emulator, backed by DuckDB, for development and CI. Point your client at
`localhost` and use dummy credentials. Queries run locally and cost nothing.

Status: **BigQuery pre-alpha**. A subset of the BigQuery REST API and SQL is implemented, with official Python client
and dbt-bigquery smoke tests in CI. APIs and supported SQL may change. **Snowflake support is planned**;
its server currently provides health checks and unsupported-endpoint errors only.

## Principles

- **Correctness over performance.** A slow, correct answer beats a fast, wrong one.
- **Unsupported means explicit.** Anything Crucible does not support raises `CrucibleUnsupportedError`. It never
  returns a silently wrong result.
- **Fidelity is measured by the corpus.** What Crucible matches is backed by recorded warehouse cases.

## Quickstart

From a repository checkout, install dependencies with [uv](https://docs.astral.sh/uv/) and start BigQuery on port 9050:

```sh
uv sync --extra server
uv run crucible serve --dialect bigquery
```

In another terminal, from the same checkout, run a query with the official Python client:

```sh
uv run --with google-cloud-bigquery python - <<'PY'
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

client = bigquery.Client(
    project="local-project",
    credentials=AnonymousCredentials(),
    client_options={"api_endpoint": "http://127.0.0.1:9050"},
)
print(next(iter(client.query("SELECT 6 * 7 AS answer").result())).answer)
client.close()
PY
```

Expected output: `42`. Anonymous credentials and the explicit endpoint keep this query local.

For Docker, run the published pre-alpha image instead of the uv server:

```sh
docker run --rm -p 127.0.0.1:9050:9050 ghcr.io/callumwalterwhite/crucible-emulator:latest serve --dialect bigquery
```

To build the image yourself, run from the repository root:

```sh
docker build -f docker/Dockerfile -t crucible:local .
docker run --rm -p 127.0.0.1:9050:9050 crucible:local serve --dialect bigquery
```

Check either server with `curl --fail http://127.0.0.1:9050/_crucible/health`.
More query, table, CSV load, and error examples are in [examples/python-bigquery/](examples/python-bigquery/README.md).
A full dbt project (seeds, views, tables, data tests) runs against Crucible in [examples/dbt-bigquery/](examples/dbt-bigquery/README.md).
Client and dbt connection details, including planned Snowflake support, are in
[docs/client-compat.md](docs/client-compat.md).

## dbt versions

Use **dbt-core 1.x with dbt-bigquery**. dbt Fusion (the `dbt` 2.x binary) works for only part of a project:

| Command | dbt-core 1.12 + dbt-bigquery 1.12 | dbt Fusion 2.0 |
| --- | --- | --- |
| `dbt seed` | works | works |
| `dbt run` | works | works |
| `dbt test` (data tests) | works | fails |
| unit tests | works | fails |

Fusion reads query results through the BigQuery Storage Read API (gRPC). Crucible does not implement that API,
and Fusion has no setting to point it at a local endpoint, so its reads go to Google. Any command that reads
rows back fails, including `dbt test` and the test step of `dbt build`. CI tests dbt-core 1.9 and the latest
1.x. Fusion details: [docs/client-compat.md](docs/client-compat.md#dbt-fusion-dbt-206-bigquery-adbc-driver-0210devdbt02119).

Fusion installs itself as `dbt` on your `PATH`. Check `dbt --version` reports Core 1.x, or call the
virtualenv's binary by path as [examples/dbt-bigquery/](examples/dbt-bigquery/README.md) does.

## What works

The full list, with a link to the test behind each feature, is in
[docs/bigquery-features.md](docs/bigquery-features.md).

- **REST API:** jobs (insert, get, cancel, query, getQueryResults), datasets and tables (list, get, insert,
  patch, delete), tabledata.list, routines (list, get), and CSV load jobs through resumable or multipart upload.
- **SQL:** each area below has corpus cases checked against real BigQuery (239 of 246 cases recorded):
  aggregates, arrays and UNNEST, casts, conditionals, dates and times, error messages, hashing, JSON, math,
  regular expressions, strings, window functions, QUALIFY, `SELECT * EXCEPT/REPLACE`, and the statements dbt
  emits (CREATE TABLE AS with partitioning and clustering, views, INSERT, MERGE, ALTER TABLE, DROP).
- **Clients:** google-cloud-bigquery for Python, dbt-core with dbt-bigquery, and dbt Fusion for seed and run.

## Current limits

Crucible is pre-alpha. Unsupported features fail with `Not supported by Crucible: ...` instead of returning a
guess. Not supported yet:

- Scripting beyond `DECLARE` and `SET` (`BEGIN ... END`, `IF`, loops), temp tables, and SQL functions
  (`CREATE FUNCTION`).
- `INFORMATION_SCHEMA` beyond the views `dbt docs generate` reads, wildcard tables (`dataset.events_*`), time travel (`FOR SYSTEM_TIME AS OF`).
- Geography functions (`ST_*`).
- The BigQuery Storage Read and Write APIs (gRPC), so `to_dataframe()` needs
  `create_bqstorage_client=False`, and dbt Fusion cannot run tests.
- CSV loads into DATETIME columns, and load formats other than CSV.
- Billing: `totalBytesProcessed` and `totalBytesBilled` are always `0`.
- Authentication and IAM: there is none (see [SECURITY.md](SECURITY.md)).

If something you need is missing, open an issue with the query and the result real BigQuery returns.

## Roadmap

1. **dbt on BigQuery:** done for the common strategies (merge, `insert_overwrite` with and without
   `copy_partitions`, `microbatch`, snapshots); next, `grants` and source freshness.
2. **Wider BigQuery:** SQL UDFs, JSON and Parquet loads, and a compatibility report generated from the
   corpus.
3. **Snowflake:** the wire protocol first, then the SQL dialect, verified the same way.

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md): setup, checks, corpus rules, and licensing.
- [docs/architecture.md](docs/architecture.md): design.
- [docs/bigquery-features.md](docs/bigquery-features.md): supported and unsupported BigQuery features, each linked to its test.
- [docs/compatibility.md](docs/compatibility.md): generated; not yet available.
- [docs/client-compat.md](docs/client-compat.md): how the official clients and dbt adapters connect to a local emulator.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
