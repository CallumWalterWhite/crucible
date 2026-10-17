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
More query, table, CSV load, and error examples are in [examples/bigquery/](examples/bigquery/README.md).
Client and dbt connection details, including planned Snowflake support, are in
[docs/client-compat.md](docs/client-compat.md).

## Security

Crucible is a local development and CI tool with **no authentication**. It binds `127.0.0.1` by default, and you must
never expose it on a network. See [SECURITY.md](SECURITY.md) for the deployment model and for how to report a
vulnerability.

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md): setup, checks, corpus rules, and licensing.
- [docs/architecture.md](docs/architecture.md): design.
- [docs/compatibility.md](docs/compatibility.md): generated; not yet available.
- [docs/client-compat.md](docs/client-compat.md): how the official clients and dbt adapters connect to a local emulator.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
