# Crucible

Crucible is a local BigQuery and Snowflake API emulator, backed by DuckDB, for development and CI. Point your existing
client or dbt profile at `localhost`, change only the endpoint, and use dummy credentials. Queries run locally and cost
nothing.

Status: pre-alpha, not yet usable.

## Principles

- **Correctness over performance.** A slow, correct answer beats a fast, wrong one.
- **Unsupported means explicit.** Anything Crucible does not support raises `CrucibleUnsupportedError`. It never
  returns a silently wrong result.
- **Fidelity is measured by the corpus.** What Crucible matches is backed by recorded warehouse cases.

## Quickstart (planned)

Every snippet below is planned. None of them work until Phase 6.

Run the BigQuery emulator on port 9050, bound to the loopback interface only:

```sh
docker run --rm -p 127.0.0.1:9050:9050 ghcr.io/OWNER/crucible-emulator:latest serve --dialect bigquery
```

Python client (planned):

```python
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

client = bigquery.Client(
    project="local-project",
    credentials=AnonymousCredentials(),
    client_options={"api_endpoint": "http://localhost:9050"},
)
```

dbt-bigquery `profiles.yml` (planned):

```yaml
local_bigquery:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: oauth-secrets
      token: dummy-token
      project: local-project
      dataset: analytics
      schema: analytics
      location: US
      api_endpoint: http://localhost:9050
      threads: 1
```

Snowflake runs on port 8080, with the same loopback rule:

```sh
docker run --rm -p 127.0.0.1:8080:8080 ghcr.io/OWNER/crucible-emulator:latest serve --dialect snowflake
```

Python connector (planned):

```python
import snowflake.connector

conn = snowflake.connector.connect(
    host="localhost",
    port=8080,
    protocol="http",
    account="local",
    user="local",
    password="dummy",
)
```

dbt-snowflake `profiles.yml` (planned):

```yaml
local_snowflake:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: local
      user: local
      password: dummy
      host: localhost
      port: 8080
      protocol: http
      database: analytics
      schema: analytics
      warehouse: local
      threads: 1
```

The client details behind these snippets are in [docs/client-compat.md](docs/client-compat.md).

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
