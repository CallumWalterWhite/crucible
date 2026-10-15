# Client compatibility

This page records how the official BigQuery and Snowflake clients, and their dbt adapters, can be pointed at a
local emulator, and what response formats and side calls they make. Every claim cites the source file and line it
was read from. Claims that could not be traced are marked `UNRESOLVED`. The page was checked on 2026-10-14.

Versions checked (oldest listed and latest on PyPI on that date):

| Package | Oldest | Latest |
|---|---|---|
| google-cloud-bigquery | 3.27.0 | 3.46.1 |
| dbt-bigquery | 1.9.0 | 1.12.1 |
| snowflake-connector-python | 3.12.0 | 4.8.0 |
| dbt-snowflake | 1.9.0 | 1.12.3 |

Transitive dependencies were read at the latest version allowed by each package's declared bounds:
google-cloud-core 2.8.0, google-auth 2.62.0, google-api-core 2.42.0, google-cloud-bigquery-storage 2.42.0.
Citations use the form `(<package>==<version>: <path inside package>:<line>)`. Paths are relative to the package
root, so `google/cloud/bigquery/client.py` means the `google.cloud.bigquery` module in that release.

Placeholders used in the snippets: BigQuery emulator on `localhost:9050`, Snowflake emulator on `localhost:8080`.

## google-cloud-bigquery (Python)

Checked at 3.27.0 (oldest) and 3.46.1 (latest). Differences are listed under item 5.

### 1. Endpoint override

- The `BIGQUERY_EMULATOR_HOST` environment variable is read when a `Client` is built. It replaces the default
  `https://bigquery.googleapis.com`. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_helpers.py:58,127-128;
  google-cloud-bigquery==3.46.1: google/cloud/bigquery/_helpers.py:61,130-131)
- The value is read into the connection's `api_endpoint` at construction. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/client.py:251-252; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:282-283)
- An explicit `client_options={"api_endpoint": ...}` (or `ClientOptions(api_endpoint=...)`) overrides the
  environment variable. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:258-260;
  google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:285-287)
- The base URL is joined as `{api_base_url}/bigquery/v2{path}`. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/_http.py:37,43,46; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_http.py:37,43,46)
- Plain `http://` is accepted by this code path: nothing checks the scheme, and the URL goes to `requests`.
  (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_http.py:37-46; google-cloud-core==2.8.0:
  google/cloud/_http/__init__.py:394) A request to a live `http://localhost` server was not run here
  `UNRESOLVED: runtime check against a plain-http server not performed`.
- The environment variable is process-wide. It applies to every `Client` built without an explicit
  `api_endpoint`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:282-283)

Minimal Python config:

```python
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

client = bigquery.Client(
    project="local-project",
    credentials=AnonymousCredentials(),
    client_options={"api_endpoint": "http://localhost:9050"},
)
```

### 2. Credential bypass

- `AnonymousCredentials` (google-auth==2.62.0: google/auth/credentials.py:575) never expires, is always valid, and
  never refreshes (google-auth==2.62.0: google/auth/credentials.py:583-595). It adds no authorization header
  (google-auth==2.62.0: google/auth/credentials.py:608-609).
- Passing `credentials` skips Application Default Credentials. (google-cloud-core==2.8.0:
  google/cloud/client/__init__.py:210-218; the ADC call is at line 218)
- Passing `project` skips project discovery. Discovery calls `google.auth.default()` only when the project is
  `None`. (google-cloud-core==2.8.0: google/cloud/client/__init__.py:307-308,
  google/cloud/_helpers/__init__.py:169-170)
- Anonymous credentials report `universe_domain` as `googleapis.com`, so the universe check passes.
  (google-auth==2.62.0: google/auth/credentials.py:80,145-147; google-cloud-bigquery==3.46.1:
  google/cloud/bigquery/_helpers.py:107)

dbt-bigquery, which methods need what:

- `method: oauth-secrets` with a `token` and no `refresh_token` works offline. The credential is built as
  `GoogleCredentials(token=...)`. (dbt-bigquery==1.9.0: dbt/adapters/bigquery/credentials.py:212-218;
  dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/credentials.py:243-250)
- That token never triggers a refresh. A token with no expiry is `FRESH` (google-auth==2.62.0:
  google/auth/credentials.py:122-127). `expired` returns `False` when there is no expiry (google-auth==2.62.0:
  google/auth/credentials.py:98-99). `valid` is `token is not None and not expired` (google-auth==2.62.0:
  google/auth/credentials.py:115), and the blocking refresh runs only when the token is not valid (google-auth==2.62.0:
  google/auth/credentials.py:203-205). `google/oauth2/credentials.py` defines neither `valid` nor `expired`
  (checked by grep).
- `method: oauth` needs Application Default Credentials. If they are missing, dbt runs
  `gcloud auth application-default login`. (dbt-bigquery==1.9.0: dbt/adapters/bigquery/clients.py:22-28,
  dbt/adapters/bigquery/credentials.py:159-172; dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/clients.py:32-38,
  src/dbt/adapters/bigquery/credentials.py:190-203)
- `method: service-account` and `service-account-json` need a real keyfile.
  (dbt-bigquery==1.9.0: dbt/adapters/bigquery/credentials.py:199-209; dbt-bigquery==1.12.1:
  src/dbt/adapters/bigquery/credentials.py:230-240) `UNRESOLVED: whether a dummy keyfile with a local token_uri can be made to work was not traced.`
- Required profile fields: `method` and `schema` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/credentials.py:135,138).

### 3. Result formats

- Results are JSON over REST. Rows are read from `f`/`v` cells. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/_helpers.py:412-417; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_helpers.py:484-489)
- `Client.query()` defaults to `api_method=INSERT`, which means `jobs.insert`. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/client.py:3386,3496; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3531,3650)
- `api_method=QUERY` uses `jobs.query` at `POST /projects/{project}/queries`. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/_job_helpers.py:277,280; google-cloud-bigquery==3.46.1:
  google/cloud/bigquery/_job_helpers.py:368,371)
- The job result is fetched with `GET /projects/{project}/queries/{job_id}`. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/client.py:2022; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2161)
- `query_and_wait` uses `jobs.query` when the request only uses allowlisted keys, and falls back to `jobs.insert`
  otherwise. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:413,514-545;
  google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:518,660-696)
- The BigQuery Storage Read API is gRPC, and is a separate service from the REST API. It is reached only through
  `to_arrow`, `to_dataframe` and the iterable variants, and only when `create_bqstorage_client` is true. That
  parameter defaults to `true`. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/table.py:1871,2057,1699-1706;
  google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:2412,2656,2100-2107)
- The storage client is created by `_ensure_bqstorage_client`. If `google-cloud-bigquery-storage` is not
  installed, it warns and returns `None`. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:557,588,601;
  google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:594,625,679)
- The default storage endpoint is `bigquerystorage.googleapis.com`. (google-cloud-bigquery-storage==2.42.0:
  google/cloud/bigquery_storage_v1/services/big_query_read/client.py:122,
  google/cloud/bigquery_storage_v1/services/big_query_read/transports/grpc.py:130)
- To avoid Storage calls, pass `create_bqstorage_client=False` to `to_arrow`/`to_dataframe`. Rows then come from
  the REST row download. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/table.py:1799-1806;
  google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:2207-2214,2412) Or do not
  install `google-cloud-bigquery-storage`.
- Arrow results over REST are opt-in and beta, and only in 3.46.1. `query_and_wait(query_results_format="ARROW")`
  sends `queryResultsFormat` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:509), and may add
  `formatOptions.arrowSerializationOptions` (same file:512). The response is read from `arrowSchema` and
  `arrowRecordBatch` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:2354,2362). Row iteration is
  disabled when the format is ARROW (google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:187-191).
  3.27.0 has no `queryResultsFormat` (checked by grep).
- dbt-bigquery does not call `to_arrow`, `to_dataframe` or `query_and_wait`. It uses `client.query()` and then
  `query_job.result()` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:570,587; dbt-bigquery==1.12.1:
  src/dbt/adapters/bigquery/connections.py:663,684). So dbt only uses REST JSON. The Storage API is never reached.

Serve JSON for REST. The Storage API is gRPC on a different host, so it is out of scope for a REST emulator unless
one of the two avoidance options above is used.

### 4. Other server calls at connect and startup

- Constructing a `Client` makes no HTTP request. It creates a `Connection` object. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/client.py:271; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:298)
- Credential and project discovery happen only when those arguments are missing. See section 2.
- No telemetry endpoint was found in the BigQuery client code. `opentelemetry_tracing.py` is optional tracing that
  needs the OpenTelemetry SDK (google-cloud-bigquery==3.27.0: google/cloud/bigquery/opentelemetry_tracing.py:21-30).
- `QuerySentEventFactory` (3.46.1 only) is a local progress callback, with no network call. (google-cloud-bigquery==3.46.1:
  google/cloud/bigquery/_job_helpers.py:133-134,139,797,811)
- dbt-bigquery `open()` only creates the client. It does not query. (dbt-bigquery==1.9.0:
  dbt/adapters/bigquery/connections.py:189-199; dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:190-200)
- `dbt debug` runs `select 1 as id`. (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:960-962; dbt-bigquery==1.12.1:
  src/dbt/adapters/bigquery/impl.py:1521-1523)
- `UNRESOLVED: OCSP or other TLS-level checks made by requests/urllib3 were not audited.`

### 5. Differences between oldest and latest

- Endpoint mechanisms (environment variable and `client_options`) are the same in both. See section 1.
- 3.46.1 adds `default_job_creation_mode`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:266;
  google/cloud/bigquery/_job_helpers.py:548-549)
- 3.27.0 reads `QUERY_PREVIEW_ENABLED` to enable `JOB_CREATION_OPTIONAL`. (google-cloud-bigquery==3.27.0:
  google/cloud/bigquery/_job_helpers.py:441-442) 3.46.1 has no such check (checked by grep).
- The `jobs.query` allowlist grows in 3.46.1 with `writeIncrementalResults`, `jobTimeoutMs`, `reservation`, `maxSlots`
  and `queryResultsFormat`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:687-692)
- The Arrow beta and `QuerySentEvent` exist only in 3.46.1. See sections 3 and 4.
- dbt-bigquery 1.9.0 requires `dbt-adapters>=1.7,<2.0` (dbt-bigquery==1.9.0: setup.py:54). 1.12.1 requires
  `dbt-adapters>=1.24.4,<2.0` (dbt-bigquery==1.12.1: pyproject.toml:27).

Minimal config for dbt-bigquery 1.12.1, which accepts `api_endpoint` in the profile
(dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/credentials.py:58; src/dbt/adapters/bigquery/clients.py:69-75):

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

For dbt-bigquery 1.9.0, which has no `api_endpoint` profile field (dbt-bigquery==1.9.0: dbt/adapters/bigquery/clients.py:59-64),
set `BIGQUERY_EMULATOR_HOST=http://localhost:9050` in the environment, and use the same profile without `api_endpoint`.

## snowflake-connector-python

Checked at 3.12.0 (oldest) and 4.8.0 (latest). Citations are relative to the importable root, which is the
`src/` directory of the sdist (so `snowflake/connector/connection.py` is the module). The two connector majors
differ in defaults, so read each item for both versions.

### 1. Endpoint override

- Connect kwargs `host`, `port`, `protocol`, `account` and `proxy_*` apply to both versions. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:160-173; snowflake-connector-python==4.8.0: snowflake/connector/connection.py:239-256)
- Defaults differ. 3.12.0: `host=127.0.0.1`, `port=8080`, `protocol=http` (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:163-170). 4.8.0: `host=127.0.0.1`, `port=443`, `protocol=https` (snowflake-connector-python==4.8.0:
  snowflake/connector/connection.py:242-253).
- The base URL is `f"{protocol}://{host}:{port}"` and every request is built from it. (snowflake-connector-python==3.12.0:
  snowflake/connector/network.py:442-443,699; snowflake-connector-python==4.8.0: snowflake/connector/network.py:457-458,744)
- If `account` is given and `host` is not, the host is derived from the account. In 3.12.0 the derived case also forces
  `port=443` and `protocol=https` when those are absent. (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1179-1185;
  snowflake-connector-python==4.8.0: snowflake/connector/connection.py:1893-1895)
- `account` must be non-empty even when `host` is given. (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1244-1249;
  snowflake-connector-python==4.8.0: snowflake/connector/connection.py:2010-2016)
- Plain `http://` is accepted: neither version validates the scheme, and 3.12.0 defaults to it. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:170; snowflake-connector-python==4.8.0: snowflake/connector/connection.py:253,1865)
  `UNRESOLVED: runtime check against a plain-http server not performed.`
- No environment variable sets host, port, protocol or account. A TOML connection profile named by
  `SNOWFLAKE_CONNECTIONS` / `SNOWFLAKE_DEFAULT_CONNECTION_NAME` is merged under explicit kwargs, so it can set them.
  (snowflake-connector-python==3.12.0: snowflake/connector/config_manager.py:149-152,465-471, snowflake/connector/connection.py:452;
  snowflake-connector-python==4.8.0: snowflake/connector/config_manager.py:171-174,523-529, snowflake/connector/connection.py:751)
- Unknown kwargs are stored as attributes, not rejected. (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1151;
  snowflake-connector-python==4.8.0: snowflake/connector/connection.py:1865)

Minimal Python config:

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

### 2. Credential bypass

- Password auth is the default. The client checks only that the password is non-empty. The login POST is always sent to
  `/session/v1/login-request`, and the server decides whether the password is valid. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:1234-1241, snowflake/connector/auth/_auth.py:192,250; snowflake-connector-python==4.8.0:
  snowflake/connector/connection.py:2000-2007, snowflake/connector/auth/_auth.py:215,285)
  `UNRESOLVED: server-side password validation is not visible in client code.`
- A user is required unless the authenticator is on an allow-list. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:1215-1221; snowflake-connector-python==4.8.0: snowflake/connector/connection.py:1939,1956-1961)
- `authenticator="oauth"` uses `AuthByOAuth`. The token goes in the login body as `AUTHENTICATOR=OAUTH` and `TOKEN`, is not
  validated client-side, and the login POST is still sent. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:1060-1063, snowflake/connector/auth/oauth.py:41-42,52-53; snowflake-connector-python==4.8.0:
  snowflake/connector/connection.py:1646-1650, snowflake/connector/auth/oauth.py:37-38,48-49)
- Passing `private_key` or `private_key_file` forces key-pair auth. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:1225; snowflake-connector-python==4.8.0: snowflake/connector/connection.py:1963-1964)
- Paths that skip `/session/v1/login-request`:
  - Session token plus master token: the connector calls `/session/heartbeat` instead of logging in. A failed heartbeat
    raises "Session and master tokens invalid" (4.8.0). (snowflake-connector-python==3.12.0:
    snowflake/connector/connection.py:274,278,978-986,1090; snowflake-connector-python==4.8.0:
    snowflake/connector/connection.py:392,396,1559-1565,1566-1576,1785)
  - 4.8.0 only, `auth_class=AuthNoAuth()`: `Auth.authenticate` returns before any HTTP call. (snowflake-connector-python==4.8.0:
    snowflake/connector/auth/__init__.py:18-32, snowflake/connector/auth/_auth.py:196-197, snowflake/connector/auth/no_auth.py:15-17)
    It is not reachable through `authenticator='NO_AUTH'`, which is rejected as unknown. (snowflake-connector-python==4.8.0:
    snowflake/connector/connection.py:1906-1928) 3.12.0 has no such class (snowflake-connector-python==3.12.0:
    snowflake/connector/auth/__init__.py:1-40).
  - 4.8.0 only, `PAT_WITH_EXTERNAL_SESSION`: no login POST. (snowflake-connector-python==4.8.0:
    snowflake/connector/connection.py:1715-1721)
- In 3.12.0 an unknown authenticator name is not rejected and falls through to Okta. (snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:1083-1088)
- 4.8.0 PAT still posts to login with `AUTHENTICATOR=PROGRAMMATIC_ACCESS_TOKEN`. (snowflake-connector-python==4.8.0:
  snowflake/connector/auth/pat.py:23-25)

### 3. Result formats

- Parameter name: `PYTHON_CONNECTOR_QUERY_RESULT_FORMAT`. (snowflake-connector-python==3.12.0:
  snowflake/connector/constants.py:331; snowflake-connector-python==4.8.0: snowflake/connector/constants.py:362)
- The client sends no format parameter when Arrow is importable. It sends `JSON` only when Arrow cannot be imported.
  (snowflake-connector-python==3.12.0: snowflake/connector/cursor.py:97,102,621-636; snowflake-connector-python==4.8.0:
  snowflake/connector/cursor.py:119,125,645-660)
- Caller-supplied `_statement_params` are merged into the request `parameters`, so `PYTHON_CONNECTOR_QUERY_RESULT_FORMAT: JSON`
  forces JSON. (snowflake-connector-python==3.12.0: snowflake/connector/cursor.py:933-944, snowflake/connector/connection.py:1312-1313;
  snowflake-connector-python==4.8.0: snowflake/connector/cursor.py:987-998, snowflake/connector/connection.py:2095-2096)
- The client reads `queryResultFormat` from the response, defaulting to `json` when absent. It handles both `json` and `arrow`.
  (snowflake-connector-python==3.12.0: snowflake/connector/cursor.py:1141, snowflake/connector/result_batch.py:187-195;
  snowflake-connector-python==4.8.0: snowflake/connector/cursor.py:1283, snowflake/connector/result_batch.py:299-308)
- JSON: the first chunk comes from `data["rowset"]`, and further chunks become `JSONResultBatch` objects. (snowflake-connector-python==3.12.0:
  snowflake/connector/result_batch.py:158-166,187-189; snowflake-connector-python==4.8.0: snowflake/connector/result_batch.py:270-278,299-301)
- Arrow: `rowsetBase64` plus `qrmk` and `chunkHeaders` build the chunk fetches. (snowflake-connector-python==3.12.0:
  snowflake/connector/result_batch.py:130-156; snowflake-connector-python==4.8.0: snowflake/connector/result_batch.py:235-268)
- Arrow-only fetch methods raise `NotSupportedError` unless the result is Arrow. (snowflake-connector-python==3.12.0:
  snowflake/connector/cursor.py:1280-1281; snowflake-connector-python==4.8.0: snowflake/connector/cursor.py:1449-1450)
- 4.8.0 only: an inline result whose row count is short is re-fetched with `GET /queries/{sfqid}/result`, at most once.
  (snowflake-connector-python==4.8.0: snowflake/connector/result_batch.py:50,88-116; snowflake/connector/cursor.py:1224-1256)
  3.12.0 has no equivalent.
- Recommendation: return `queryResultFormat: "json"` explicitly. Both versions honour it, and it avoids depending on
  the server default. `UNRESOLVED: the server-side default for queryResultFormat when the client sends no parameter is not determinable from client code.`

### 4. Other server calls

- Login response parameters are applied to the connection. (snowflake-connector-python==3.12.0:
  snowflake/connector/auth/_auth.py:477-482; snowflake-connector-python==4.8.0: snowflake/connector/auth/_auth.py:549-553)
- Heartbeat `POST /session/heartbeat` runs only when `client_session_keep_alive` is truthy. The server parameter
  `CLIENT_SESSION_KEEP_ALIVE` fills it in only when the local value is `None`, which is the default. Disable with
  `client_session_keep_alive=False`. (snowflake-connector-python==3.12.0: snowflake/connector/network.py:621,
  snowflake/connector/connection.py:206,1097-1101,1726-1730; snowflake-connector-python==4.8.0: snowflake/connector/network.py:664,
  snowflake/connector/connection.py:317,1792-1796,2543-2547)
- Session delete `POST /session?delete=true` runs at `close()`. Disable with `server_session_keep_alive=True`.
  (snowflake-connector-python==3.12.0: snowflake/connector/network.py:632,646, snowflake/connector/connection.py:266-267,764-786;
  snowflake-connector-python==4.8.0: snowflake/connector/network.py:675,689, snowflake/connector/connection.py:384-385,1346-1373)
- In-band telemetry `POST /telemetry/send` (snowflake-connector-python==3.12.0: snowflake/connector/telemetry.py:131,185-186;
  snowflake-connector-python==4.8.0: snowflake/connector/telemetry.py:148,202-203).
  - 3.12.0: `telemetry_enabled` starts `False` and becomes true only when the server sends `CLIENT_TELEMETRY_ENABLED`.
    (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:412,1719-1720)
  - 4.8.0: needs the client flag and the server flag both true. (snowflake-connector-python==4.8.0:
    snowflake/connector/connection.py:683-684,1145-1149,2541-2542)
  - Disable: `conn.telemetry_enabled = False` after connect. (snowflake-connector-python==3.12.0:
    snowflake/connector/connection.py:631-633; snowflake-connector-python==4.8.0: snowflake/connector/connection.py:1151-1153)
  - Out-of-band telemetry is hard-disabled in both versions: `TelemetryService.enabled` is fixed to `False`, so the upload POST is never reached.
    (snowflake-connector-python==3.12.0: snowflake/connector/telemetry_oob.py:193-199,274-276; snowflake-connector-python==4.8.0:
    snowflake/connector/telemetry_oob.py:189-195,270-272)
- OCSP certificate checks:
  - 3.12.0 defaults to `ocsp_fail_open=True`, and OCSP validation runs unless `insecure_mode` is set. (snowflake-connector-python==3.12.0:
    snowflake/connector/connection.py:201-202,474-482; snowflake/connector/ssl_wrap_socket.py:84-92)
  - 4.8.0 defaults to disabled. (snowflake-connector-python==4.8.0: snowflake/connector/connection.py:305-309,712-730;
    snowflake/connector/_ocsp_mode.py:30-62; snowflake/connector/ssl_wrap_socket.py:548)
  - Env vars: `SF_OCSP_RESPONSE_CACHE_SERVER_ENABLED=false` disables the cache server, and `SF_OCSP_RESPONSE_CACHE_SERVER_URL` overrides its URL.
    (snowflake-connector-python==3.12.0: snowflake/connector/ocsp_snowflake.py:287-295,298; snowflake-connector-python==4.8.0:
    snowflake/connector/ocsp_snowflake.py:460-478,481)
  `UNRESOLVED: whether OCSP runs for protocol=http, which has no TLS handshake, was not traced.`
- Query and cancel endpoints: `POST /queries/v1/query-request` (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1342;
  snowflake-connector-python==4.8.0: snowflake/connector/connection.py:2125), `POST /queries/v1/abort-request` on timeout or SIGINT
  (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1625-1631, snowflake/connector/cursor.py:656-684;
  snowflake-connector-python==4.8.0: snowflake/connector/connection.py:2441-2451, snowflake/connector/cursor.py:704-725),
  `GET /monitoring/queries/<sfqid>` (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1773-1794;
  snowflake-connector-python==4.8.0: snowflake/connector/connection.py:2598-2619), and `GET /queries/{qid}/result`
  (snowflake-connector-python==3.12.0: snowflake/connector/cursor.py:1240-1241; snowflake-connector-python==4.8.0: snowflake/connector/cursor.py:1392-1394).
- Session renewal `POST /session/token-request` runs on GS code 390112 ("session expired"). (snowflake-connector-python==3.12.0:
  snowflake/connector/network.py:540-555,704-709; snowflake-connector-python==4.8.0: snowflake/connector/network.py:583-598,751-759)
  No connect kwarg disables it.
- The connector issues no `ALTER SESSION` or `USE` statements itself.

### 5. Differences, 3.12.0 to 4.8.0

- Defaults: port 8080 over http becomes port 443 over https, and the account-derived port/protocol forcing exists only in 3.12.0
  (see items 1 and 4).
- Auth: 4.8.0 adds `AuthNoAuth` (Python `auth_class` only) and `PAT_WITH_EXTERNAL_SESSION`. Both versions skip login with session and master tokens. (see section 2)
- OCSP defaults: fail-open in 3.12.0, disabled in 4.8.0. 4.8.0 adds CRL, default disabled (snowflake-connector-python==4.8.0:
  snowflake/connector/crl.py:37-38,54-55).
- Telemetry: 4.8.0 requires both the client and server flags.
- Inline-result refetch exists only in 4.8.0.

## dbt-snowflake

Checked at 1.9.0 and 1.12.3. The two dbt versions pin different connector majors: 1.9.0 requires
`snowflake-connector-python[secure-local-storage]~=3.0` (dbt-snowflake==1.9.0: setup.py:62), and 1.12.3 requires
`>=4.2.0,<5.0.0` (dbt-snowflake==1.12.3: pyproject.toml:29). So 1.9.0 runs on connector 3.x and 1.12.3 on connector 4.x.

### 1. Endpoint override

- The profile fields `host`, `port`, `protocol`, `proxy_host` and `proxy_port` are forwarded to the connector only when truthy.
  (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:192-201; dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:232-241)
- `account` is always passed, with `_` replaced by `-`. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:142,381;
  dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:174,460)
- Connector call: (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:380-391; dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:459-473)
- Consequence for connector 3.x: `account` is always passed, so if the profile sets neither `port` nor `protocol`, the
  connector forces `443` and `https` (snowflake-connector-python==3.12.0: snowflake/connector/connection.py:1179-1185).
  A local profile must set `protocol: http` and `port`.
- The OAuth refresh-token exchange POSTs to `https://{account}.snowflakecomputing.com/oauth/token-request` and ignores the
  profile `host`. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:70,208-209,274; dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:85,248-249,350)
- dbt reads no environment variable for host or port. (checked by grep)

Minimal `profiles.yml`, which works for both dbt versions:

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

### 2. Credential bypass

- `auth_args()` builds the connector kwargs one by one. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:184-203;
  dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:224-243)
- `password` is forwarded only when truthy. If it is empty and no authenticator is set, the connector raises "Password is empty".
  (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:190-191; snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:1241; snowflake-connector-python==4.8.0: snowflake/connector/connection.py:2007)
- `user` is required unless the authenticator is `jwt` or `oauth` in 1.9.0 (dbt-snowflake==1.9.0:
  dbt/adapters/snowflake/connections.py:136-140). 1.12.3 also allows `programmatic_access_token` and `workload_identity`
  (dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:153-170).
- Authenticators. 1.9.0: `oauth` (a token, or a refresh-token exchange), and `jwt` mapped to `oauth` with a token. Any other string
  is forwarded unchanged (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:202-230). 1.12.3 adds
  `programmatic_access_token` (dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:272-276) and `workload_identity`
  (dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:278-306).
- Private key: `_get_private_key` (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:298-308; dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:374-384). Setting both `private_key` and `private_key_path` is a config error.
- No profile field forwards `session_token`, `master_token` or `auth_class`, so the connector's login-free paths are
  unreachable from dbt. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:88-114; dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:105-133)
- Dummy credentials therefore mean password auth with any non-empty `password`. The login POST still goes to the emulator.

### 3. Result formats

- dbt sets no statement parameters. Queries run as `cursor.execute(sql, bindings)` (dbt-adapters==1.24.5:
  dbt/adapters/sql/connections.py:97), and dbt-snowflake's `add_query` delegates to the base
  (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:525; dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:629).
  The connector default therefore applies (section 3 of the connector).
- No profile field forwards statement parameters, so dbt cannot force JSON through the profile. Serve JSON from the
  server, as described in the connector section.
- `UNRESOLVED: dbt-snowflake 1.9.0 requires dbt-adapters>=1.10.4 (dbt-snowflake==1.9.0: PKG-INFO:21), which is not in the
  fetched tree. The cursor.execute citation is from dbt-adapters==1.24.5.`

### 4. Other server calls

- `open()` only calls `snowflake.connector.connect(...)` and runs no SQL. Session parameters carry only `QUERY_TAG` when set,
  and in 1.12.3 `S3_STAGE_VPCE_DNS_NAME`. These are login-time session parameters, not `ALTER SESSION`.
  (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:359-399,368-371; dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:435-480,444-450)
- `dbt debug` runs `select 1 as id`. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/impl.py:431-432; dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/impl.py:637-638)
- Warehouse switching runs `select current_warehouse()` and `use warehouse <name>`, but only when a model sets
  `snowflake_warehouse` to a value different from the profile. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/impl.py:128-150;
  dbt-snowflake==1.12.3: dbt/adapters/snowflake/impl.py:246-270)
- Query tag SQL (`show parameters like 'query_tag' in session`, `alter session set query_tag`, `alter session unset query_tag`)
  runs only when a model sets the `query_tag` config. (dbt-snowflake==1.9.0: dbt/include/snowflake/macros/adapters.sql:229-264;
  dbt-snowflake==1.12.3: dbt/include/snowflake/macros/adapters.sql:143-178)
- dbt forwards `client_session_keep_alive` on every connect, default False, so the connector sends no heartbeat from dbt.
  (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:102,387; dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:119,466)
  The session is deleted at close (section 4 of the connector).
- `insecure_mode` is a profile field and defaults to False. With connector 3.x that means OCSP fail-open. Set
  `insecure_mode: true` in a local profile to skip OCSP. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:112,389;
  dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:129,468; snowflake-connector-python==3.12.0:
  snowflake/connector/connection.py:201,474-482)
- Dbt's `reuse_connections` defaults to True when `client_session_keep_alive` is False. It is not a connector parameter, so the
  connector just stores it as an attribute. (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:146-147;
  dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:182-183)

### 5. Differences, 1.9.0 to 1.12.3

- Connector major: 3.x (1.9.0) and 4.x (1.12.3). Connector defaults are in the connector section.
- Auth surface: 1.12.3 adds `programmatic_access_token` and `workload_identity` (dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:272-306). It also allows no user for them (dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:153-170).
- Connect kwargs: 1.12.3 adds `platform_detection_timeout_seconds` (dbt-snowflake==1.12.3:
  dbt/adapters/snowflake/connections.py:135-139,469) and `ocsp_root_certs_dict_lock_timeout=10`
  (dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:471).
- Account normalisation: 1.9.0 uses `str.replace` (dbt-snowflake==1.9.0: dbt/adapters/snowflake/connections.py:142), and 1.12.3 uses
  `re.subn` with a debug log (dbt-snowflake==1.12.3: dbt/adapters/snowflake/connections.py:174).
- `UNRESOLVED: the connector endpoints behind platform_detection_timeout_seconds were not traced.`

## BigQuery REST calls used by the clients

Source reading only, no live calls. Versions: google-cloud-bigquery 3.27.0, 3.45.2 and 3.46.1 (3.45.2 and 3.46.1
match in the job, query and table modules); dbt-bigquery 1.9.0 and 1.12.1; google-api-core 2.42.0; google-cloud-core
2.8.0; google-resumable-media 2.11.0. What the server must do is taken from what the client reads and waits for. It is
not a claim about the server's own behaviour, which the UNRESOLVED items list.

The table gives the field lists per endpoint. The detail below cites each claim.

| REST call | Client methods (3.46.1) | Query parameters and body the client sends | Response fields the client reads | Retry and polling a server must satisfy |
|---|---|---|---|---|
| `jobs.insert` POST `/projects/{p}/jobs` | `Job._begin`; `Client.query()` (default `api_method=INSERT`) | Job resource body; client-minted `jobId` | Whole body; `status.state`, `status.errorResult`, `statistics.*Time` | `DEFAULT_RETRY`; on 409 with the same id, `get_job` |
| `jobs.get` GET `/projects/{p}/jobs/{id}` | `get_job`, `Job.reload`, `Job.done` | `projection=full`; `location` | `status.state`, `status.errorResult`, `configuration` (picks the job class) | `QueryJob.result()` busy-waits on `done()` until `state == DONE` (no sleep); `DEFAULT_JOB_RETRY` |
| `jobs.getQueryResults` GET `/projects/{p}/queries/{id}` | `_get_query_results`, `QueryJob._reload_query_results`, `_list_rows_from_query_results` | `maxResults`; `formatOptions.useInt64Timestamp` (page size > 0); `location`; `startIndex`; `timeoutMs` only if set (never on the `result()` path); `pageToken`, `fields` when paging | `jobComplete`, `jobReference`, `pageToken`, `totalRows`, `numDmlAffectedRows`, `rows`, `schema` | Must return `jobComplete=true` eventually; `DEFAULT_RETRY` per call |
| `jobs.query` POST `/projects/{p}/queries` | `query_and_wait` (3.45.2, 3.46.1); `Client.query(api_method=QUERY)` | `useLegacySql`, `formatOptions.useInt64Timestamp`, `timeoutMs`, `location`, `query`, `maxResults`, `requestId`; `jobCreationMode` only when `default_job_creation_mode` is set | `jobComplete`, `pageToken`, `jobReference` (required when incomplete or paged), `schema`, `totalRows`, `rows` | `DEFAULT_RETRY` per POST; whole call under `job_retry`; an incomplete response is polled as a job |
| `tables.get` GET `.../tables/{t}` | `get_table` | None | `tableReference.tableId` (KeyError if absent); `schema.fields` (absent gives []) | `DEFAULT_RETRY`; 404 not retried |
| `tables.list` GET `.../tables` | `list_tables` | `pageToken`, `maxResults` | `tables[]` (each needs `tableReference`); `nextPageToken` | Paged on `nextPageToken`; `DEFAULT_RETRY` |
| `tables.patch` PATCH `.../tables/{t}` | `update_table` | Only the listed fields; `If-Match` with the etag; `autodetect_schema=true` (3.45.2 and later) | Response through `Table.from_api_repr` | `DEFAULT_RETRY`; 412 not retried |
| `tables.delete` DELETE `.../tables/{t}` | `delete_table` | None | Body not read | `DEFAULT_RETRY`; 404 swallowed only with `not_found_ok` |
| `routines.list` GET `.../routines` | `list_routines` | `pageToken`, `maxResults` | `routines[]` (each needs `routineReference`); `nextPageToken` | `DEFAULT_RETRY` |
| `routines.get` GET `.../routines/{r}` | `get_routine` | None | `Routine.from_api_repr` | `DEFAULT_RETRY` |
| `datasets.list` GET `/projects/{p}/datasets` | `list_datasets` | `all`, `filter`, `pageToken`, `maxResults` | `datasets[]` (each needs `datasetReference`) | `DEFAULT_RETRY` |
| `datasets.get` GET `/projects/{p}/datasets/{d}` | `get_dataset` | `datasetView` when given | `datasetReference.datasetId` (KeyError if absent) | `DEFAULT_RETRY` |
| `datasets.insert` POST `/projects/{p}/datasets` | `create_dataset` | Dataset resource; `location` from the client default when absent | `Dataset.from_api_repr` | `DEFAULT_RETRY`; 409 raised unless `exists_ok` |
| `datasets.delete` DELETE `/projects/{p}/datasets/{d}` | `delete_dataset` | `deleteContents=true` when requested | Body not read | `DEFAULT_RETRY`; 404 swallowed only with `not_found_ok` |
| Resumable load `POST /upload/bigquery/v2/projects/{p}/jobs?uploadType=resumable`, then `PUT` | `load_table_from_file` (size unknown or ≥ 5 MiB) | Initiate with the job resource; `PUT` chunks with `Content-Range`; chunk size 100 MiB | Initiate: `Location` header; final 200 body becomes the job | Retries 408, 429 and 5xx; BigQuery never calls `recover()` |

### Per-endpoint detail

### Shared transport (applies to every endpoint)

- Every call goes through `Client._call_api` (bigquery client.py:896-917), which calls `Connection.api_request` with the retry wrapper. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:896)
- Base URL and API version: `https://bigquery.googleapis.com`, `v2`, template `{api_base_url}/bigquery/{api_version}{path}`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_http.py:32)
- Every URL gets `prettyPrint=false` added. (google-cloud-core==2.8.0: google/cloud/_http/__init__.py:285)
- A non-None `timeout` kwarg is also sent as header `X-Server-Timeout`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4629)
- `DEFAULT_TIMEOUT = None`; every caller passes `timeout=timeout`, which overrides core's default of 60 s, so the HTTP call has no timeout unless the caller sets one (code reading). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:131; google-cloud-core==2.8.0: google/cloud/_http/__init__.py:410)
- Non-2xx responses raise `exceptions.from_http_response`. An empty body is returned as bytes, not JSON. (google-cloud-core==2.8.0: google/cloud/_http/__init__.py:509; google-cloud-core==2.8.0: google/cloud/_http/__init__.py:511)
- `DEFAULT_RETRY` retries only when `errors[0].reason` is in {rateLimitExceeded, backendError, internalError, badGateway}, or when there is no structured reason and the exception is one of: ConnectionError, TooManyRequests (429), InternalServerError (500), BadGateway (502), ServiceUnavailable (503), requests ChunkedEncodingError/ConnectionError/Timeout, auth TransportError. SSLError is excluded. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:25; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:29; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:46; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:69)
- `DEFAULT_RETRY` deadline is 600 s. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:41; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:90)
- 409 Conflict, 404 NotFound and 412 are not in the retry reason set or the unstructured types, so `DEFAULT_RETRY` does not retry them. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:25; google-api-core==2.42.0: google/api_core/exceptions.py:328)
- Retry defaults inside api_core's `Retry` are overridden by BigQuery's deadline; initial 1 s, max 60 s, multiplier 2. (google-api-core==2.42.0: google/api_core/retry/retry_base.py:42)


#### 1. jobs.insert  POST /projects/{p}/jobs

- (a) Client methods: `Job._begin` sends POST `/projects/%s/jobs` with body `self.to_api_repr()` and no query params. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:820; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:830). Query path: `client.query()` with the default `api_method=INSERT` calls `query_jobs_insert`, which calls `_begin`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3649; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:151). The job ID is client-generated (`make_job_id`). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:77). Same POST and path in 3.27.0. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/job/base.py:741)
- (b) Response fields read: the whole body is stored by `_set_properties`. `statistics.creationTime/startTime/endTime` are cast to float. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:744). `status.errorResult` and `status.errors` are read (error_result / errors). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:695; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:711). `status.state` is read; if `status` is absent, state is None and the job is not done. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:729). If state is None when `result()` runs, it calls `_begin` again. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1712)
- (c) Retry and 409: `_begin` uses `DEFAULT_RETRY`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:796). On 409 Conflict with a client-generated job ID, `query_jobs_insert` calls `get_job` for the same ID (retry predicate: NotFound or DEFAULT_RETRY, deadline 200 s). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:162; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:166; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:183; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:101; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:118). The 404-after-409 case is a comment in the package citing an issue. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:170). Whole do_query is wrapped in `_DEFAULT_QUERY_JOB_INSERT_RETRY` when `job_retry` is set. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:203; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:186). In 3.27.0 the Conflict path calls `get_job` with the caller's retry and has no conflict-specific predicate. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:137; google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:145)
- (d) n/a.
- (e) n/a.

#### 2. jobs.get  GET /projects/{p}/jobs/{jobId}

- (a) `Client.get_job`: query params `projection=full` always, `location` if set (client default or argument). Path `/projects/{p}/jobs/{id}`, method GET. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2325; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2338; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2340; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2348). 3.27.0 and 3.45.2 send the same `projection=full` and `location` (3.27.0: cloud/bigquery/client.py:2186, 2199; 3.45.2: cloud/bigquery/client.py:2325, 2338). Callers: `Job.reload` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:908) and `Job.done` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:1014).
- (b) `job_from_resource` picks the class from `configuration` keys load/copy/extract/query; with no configuration, it returns UnknownJob. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2190; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2178). `status.state` is read as in 1(b).
- (c) Polling: `QueryJob.result` loops `is_job_done()`. Each iteration calls `done()`, which calls `reload()` (jobs.get) while state != "DONE", and then calls getQueryResults. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1689; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1720; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/base.py:1014). The loop ends when done with no error (state DONE and no `errorResult`). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1745). The loop has no sleep: `while not is_job_done(): pass`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1797). With a timeout, it uses a monotonic clock and raises concurrent.futures.TimeoutError. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1801). `get_job` default timeout is 128 s. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2294; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:213). The loop is wrapped in `DEFAULT_JOB_RETRY`, which retries on jobBackendError, jobInternalError, jobRateLimitExceeded; deadline 2400 s. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1773; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:138; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:66).
- Note: the `job_retry` default is on the `QueryJob.result` signature. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1576)

#### 3. jobs.getQueryResults  GET /projects/{p}/queries/{jobId}

- (a) `Client._get_query_results`: `maxResults` always; `formatOptions.useInt64Timestamp=true` only when page_size > 0; `timeoutMs` only if `timeout_ms` is passed; `location`; `startIndex` if given; path `/projects/{p}/queries/{id}`; GET. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2135; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2144; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2150; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2156; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2159; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2161). The transport timeout is floored at 120 s. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2141; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:167).
- Callers: `QueryJob._reload_query_results` (maxResults=0 while waiting, since page_size defaults to 0). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1498; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1558). It skips the call when cached results are complete. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1522). `timeoutMs` is set only when `QueryJob._done_timeout` is not None. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1539; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1545). The only writer of `_done_timeout` after init is `_blocking_poll`, which has no caller in the package. So on the `result()` path, `timeoutMs` is not sent (code reading). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:904; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1433)
- Row paging: `_list_rows_from_query_results` sends `fields=jobReference,totalRows,pageToken,rows`, `location`, `startIndex`, `formatOptions.useInt64Timestamp=true`, path `/projects/{p}/queries/{job}`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4373; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4384; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4386; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4390; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:159). RowIterator uses the first page inline when present, then requests further pages with pageToken. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:2146; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:2153).
- 3.27.0 `_get_query_results` has no `startIndex` parameter; `_list_rows_from_query_results` does. (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:4143; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2159).
- (b) Response fields read by `_QueryResults`: `jobComplete` (complete), `jobReference.jobId/projectId/location`, `pageToken`, `totalRows`, `numDmlAffectedRows`, `rows`, `schema`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1185; google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1213; google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1252; google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1264; google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1306; google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1370; google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1383). Absent behaviour: `jobComplete` absent gives None, which is treated as incomplete (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:599); absent `jobId` gives None (google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1213); absent `pageToken` gives None (google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1252); absent `totalRows` gives None, and `QueryJob.result` then returns an empty iterator (DDL/DML) (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1827); absent `rows` gives () (google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1380); absent `schema` gives no fields (google-cloud-bigquery==3.46.1: google/cloud/bigquery/schema.py:572). `numDmlAffectedRows` from the job statistics is at google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1271.
- (c) Polling: the server must return `jobComplete=true` eventually; the package says getQueryResults "can hang up to the timeout value (default of 10 seconds)". (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1525). Retry: `DEFAULT_RETRY` passed from `result()`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1573). Each loop iteration calls it once. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1757; google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1766).

#### 4. jobs.query  POST /projects/{p}/queries

- (a) `Client.query_and_wait` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3665) calls `_job_helpers.query_and_wait` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3800). Body from `_to_query_request`: `useLegacySql=false` default; `formatOptions.useInt64Timestamp=true` (not for PICOSECOND); `timeoutMs = 1000*timeout - 250`; `location`; `query`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:288; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:298; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:302; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:305; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:74). Added in query_and_wait: `maxResults` (545-547), `jobCreationMode` (548-549, see (e)), `requestId` per attempt (555), `queryResultsFormat` and `formatOptions.arrowSerializationOptions` (509-512). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:545; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:555; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:509). Path `/projects/{p}/queries` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:367). POST at each attempt (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:571).
- Fallback to jobs.insert when the body has a key outside the allowlist (`queryResultsFormat` is in the list) (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:518; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:660; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:692).
- `Client.query(api_method=QUERY)` calls `query_jobs_query`, which sends `requestId` and no `jobCreationMode`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3637; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:396).
- 3.27.0 differences: `useInt64Timestamp` and `timeoutMs` are sent in `_to_query_request` (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:207; google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:211); `maxResults` (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:437); `requestId` (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:445); no `queryResultsFormat` or arrow options (grep: none in 3.27.0).
- (b) Response: `_QueryResults.from_api_repr` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:593). Reads `pageToken` (596) and `jobComplete` (599). If there are more pages or it is incomplete, it goes to `_to_query_job` and then `_wait_or_cancel` (600-603). Otherwise it returns a RowIterator built from `schema`, `totalRows`, `numDmlAffectedRows`, `jobId`, `location`, `queryId`, and `first_page_response` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:631). Absent-field behaviour is as in section 3(b).
- (c) Retry: each POST is wrapped in `retry` (DEFAULT_RETRY) (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:571). The whole call is wrapped in `job_retry`, which defaults to `DEFAULT_JOB_RETRY` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3675; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:654). The server must honour `timeoutMs`: an incomplete response leads to job polling via `_wait_or_cancel` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:699; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:738). On failure while waiting, the job is cancelled (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:757).
- (e) jobCreationMode and jobReference:
  - 3.45.2 and 3.46.1 send `jobCreationMode` only when `Client(default_job_creation_mode=...)` is truthy (default None). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:548; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:266; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:301; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:312; google-cloud-bigquery==3.46.1: google/cloud/bigquery/enums.py:454; google-cloud-bigquery==3.46.1: google/cloud/bigquery/enums.py:457). Same in 3.45.2 (google-cloud-bigquery==3.45.2: google/cloud/bigquery/_job_helpers.py:548).
  - Response with no `jobReference` and complete with no `pageToken`: returns a RowIterator with `job_id=None`, with no error. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/query.py:1213; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:631).
  - Response with no `jobReference` and incomplete or with `pageToken`: `_to_query_job` reads `query_response["jobReference"]` and raises a bare KeyError. The retry predicate does not retry it (no `.errors`). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:318; google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:145). The package comment says the job ID is always present when more pages exist. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:591).
  - `query_jobs_query` has the same KeyError path (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:318; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:398), but it never sends `jobCreationMode`.
  - Package docs: OPTIONAL "sometimes doesn't create a job at all"; REQUIRED "always includes a job ID" (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:20; google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:22).
  - Changes 3.27.0 to 3.46.1: 3.27.0 sends `jobCreationMode=JOB_CREATION_OPTIONAL` only when env `QUERY_PREVIEW_ENABLED=true` (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:441; google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:442), and its docstring says it will always use OPTIONAL once GA (google-cloud-bigquery==3.27.0: google/cloud/bigquery/_job_helpers.py:340). It has no client-level setting (grep count 0 for `default_job_creation_mode` in 3.27.0 client.py). 3.45.2 replaces the env var with the `Client` argument (google-cloud-bigquery==3.45.2: google/cloud/bigquery/client.py:266; google-cloud-bigquery==3.45.2: google/cloud/bigquery/_job_helpers.py:548). `queryResultsFormat`/arrow options first appear in 3.45.2 (google-cloud-bigquery==3.45.2: google/cloud/bigquery/_job_helpers.py:509).

#### 5. tables.get  GET /projects/{p}/datasets/{d}/tables/{t}

- (a) `Client.get_table`: path = `Table.path` (`/projects/{p}/datasets/{d}/tables/{t}`), GET, no query params, timeout passed through. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1262; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1268; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:232). Dbt calls: section "dbt".
- (b) `Table.from_api_repr` requires `tableReference.tableId` and raises KeyError if missing. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1325; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1344). Schema is read from `schema.fields`; absent schema gives []. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:619; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:620). Other fields are kept raw.
- (c) `DEFAULT_RETRY`. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1235). 404 is not retried (not in set).

#### 6. tables.list  GET /projects/{p}/datasets/{d}/tables

- (a) `Client.list_tables`: path `"%s/tables" % dataset.path`; HTTPIterator with `items_key="tables"`, page_token, max_results, page_size; no extra query params. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1783; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1801). The api_core iterator sends `pageToken`, `maxResults` (google-api-core==2.42.0: google/api_core/page_iterator.py:405; google-api-core==2.42.0: google/api_core/page_iterator.py:416). 3.27.0 uses `items_key="tables"` as well (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:1669).
- (b) Items are read from `tables` (default items key would be "items"); missing gives (). Each item is wrapped as TableListItem, which raises ValueError if `tableReference` is missing. (google-api-core==2.42.0: google/api_core/page_iterator.py:374; google-api-core==2.42.0: google/api_core/page_iterator.py:319; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4540; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1453). `nextPageToken` is read; absent ends iteration. (google-api-core==2.42.0: google/api_core/page_iterator.py:322; google-api-core==2.42.0: google/api_core/page_iterator.py:377).
- (c) `DEFAULT_RETRY` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1738). Paging is driven by `nextPageToken`.

#### 7. tables.patch  PATCH /projects/{p}/datasets/{d}/tables/{t}

- (a) `Client.update_table`: body is `table._build_resource(fields)`, containing only the listed fields. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1552; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1383). `If-Match: <etag>` header when the table has an etag. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1554). Query param `autodetect_schema=true` only when the argument is True (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1561; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1562). Path `table.path`, method PATCH. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1558; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1570). 3.27.0 has no `autodetect_schema` parameter (grep); 3.45.2 has it (google-cloud-bigquery==3.45.2: google/cloud/bigquery/client.py:1507).
- (b) Response read via `Table.from_api_repr` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1577; google-cloud-bigquery==3.46.1: google/cloud/bigquery/table.py:1325).
- (c) `DEFAULT_RETRY` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1508). A 412 from a stale etag is not in the retry set and is not retried (google-cloud-bigquery==3.46.1: google/cloud/bigquery/retry.py:25).

#### 8. tables.delete  DELETE /projects/{p}/datasets/{d}/tables/{t}

- (a) `Client.delete_table`: path `table.path`, DELETE, no query params (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2081; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2087).
- (b) Response body is not read.
- (c) `DEFAULT_RETRY`. NotFound is swallowed only when `not_found_ok` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2091; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2092). Empty body is accepted (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2087; google-cloud-core==2.8.0: google/cloud/_http/__init__.py:511).

#### 9a. routines.list  GET /projects/{p}/datasets/{d}/routines

- (a) `Client.list_routines`: path `"{}/routines".format(dataset.path)`, HTTPIterator, `items_key="routines"`, page_token, max_results, page_size. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1706; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1725). 3.27.0 same key (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:1593).
- (b) Items go through `_item_to_routine` -> `Routine.from_api_repr`, which indexes `routineReference` (KeyError if absent). (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4526; google-cloud-bigquery==3.46.1: google/cloud/bigquery/routine/routine.py:396).
- (c) `DEFAULT_RETRY` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1661). Paging on `nextPageToken`.

#### 9b. routines.get  GET /projects/{p}/datasets/{d}/routines/{r}

- (a) `Client.get_routine`: path `routine_ref.path` (`/projects/%s/datasets/%s/routines/%s`), GET, no query params. (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1220; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1226; google-cloud-bigquery==3.46.1: google/cloud/bigquery/routine/routine.py:568).
- (b) `Routine.from_api_repr` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1230; google-cloud-bigquery==3.46.1: google/cloud/bigquery/routine/routine.py:385).
- (c) `DEFAULT_RETRY` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1191).

#### 10a. datasets.list  GET /projects/{p}/datasets

- (a) `Client.list_datasets`: `all=true` if `include_all` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:522; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:523); `filter` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:527); path `/projects/%s/datasets` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:528); HTTPIterator with `items_key="datasets"` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:547). 3.27.0 has the same `all` and `filter` (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:486; google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:490).
- (b) Items become `DatasetListItem` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4484; google-cloud-bigquery==3.46.1: google/cloud/bigquery/dataset.py:1094 raises ValueError if `datasetReference` is missing).
- (c) `DEFAULT_RETRY`, `nextPageToken` paging.

#### 10b. datasets.get  GET /projects/{p}/datasets/{d}

- (a) `Client.get_dataset`: path `dataset_ref.path`; `datasetView` if given; GET (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:965; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:968; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:977; google-cloud-bigquery==3.46.1: google/cloud/bigquery/dataset.py:118).
- (b) `Dataset.from_api_repr` raises KeyError if `datasetReference.datasetId` is missing (google-cloud-bigquery==3.46.1: google/cloud/bigquery/dataset.py:1022; google-cloud-bigquery==3.46.1: google/cloud/bigquery/dataset.py:1034; google-cloud-bigquery==3.46.1: google/cloud/bigquery/dataset.py:1037).
- (c) `DEFAULT_RETRY` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:922).

#### 11. datasets.insert  POST /projects/{p}/datasets

- (a) `Client.create_dataset`: path `/projects/%s/datasets` % dataset.project (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:756); body `dataset.to_api_repr()`, with `location` filled from the client default when absent (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:760); POST (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:769). No query params.
- (b) Response read by `Dataset.from_api_repr` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:774).
- (c) `DEFAULT_RETRY`. 409 is raised unless `exists_ok`, in which case it falls back to get_dataset (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:775; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:778).

#### 12. datasets.delete  DELETE /projects/{p}/datasets/{d}

- (a) `Client.delete_dataset`: `deleteContents=true` only when `delete_contents` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1850); path `dataset.path` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1848); DELETE (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1860).
- (b) Body not read.
- (c) `DEFAULT_RETRY`. NotFound swallowed only with `not_found_ok` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1865; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:1866).

#### 13. Resumable load upload  POST /upload/bigquery/v2/projects/{p}/jobs?uploadType=resumable, then PUT to Location

- (a) Client path: `load_table_from_file` builds `job_resource = load_job.to_api_repr()` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2723). Resumable is used when `size is None or size >= 5 MiB` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2731; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:147). Upload URL template `{host}/upload/bigquery/v2/projects/{project}/jobs?uploadType=resumable` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:149; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:151; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3216). Chunk size 100 MiB (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:146; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3202). Headers: Accept, Accept-Encoding, User-Agent, content-type (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:4621; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3204). `num_retries` default 6, becomes `RetryStrategy(max_retries=...)` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:148; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3223). Initiate uses `stream_final=False` and content type `*/*` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3227; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:152). Chunks: loop on `transmit_next_chunk` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3162). Final body: `response.json()` -> `job_from_resource` (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2742). 3.27.0 has the same constants and call sites (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:138; google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:3018; google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:3082).
- (d) Protocol as google-resumable-media 2.11.0 implements it:
  - Initiate: POST to the upload URL (google-resumable-media==2.11.0: google/resumable_media/_upload.py:56; google-resumable-media==2.11.0: google/resumable_media/_upload.py:425). Headers: BigQuery base headers plus `content-type: application/json; charset=UTF-8` and `x-upload-content-type: */*` (google-resumable-media==2.11.0: google/resumable_media/_upload.py:482; google-resumable-media==2.11.0: google/resumable_media/_upload.py:483). `x-upload-content-length` is set only when the total is known (google-resumable-media==2.11.0: google/resumable_media/_upload.py:486; google-resumable-media==2.11.0: google/resumable_media/_upload.py:493). Body is `json.dumps(metadata)`, the job resource (google-resumable-media==2.11.0: google/resumable_media/_upload.py:495).
  - Initiate response: status 200 or 201 required; `Location` header required and stored as the resumable URL (google-resumable-media==2.11.0: google/resumable_media/_upload.py:514; google-resumable-media==2.11.0: google/resumable_media/_upload.py:520). The package says the upload_id in that URL is not checked (google-resumable-media==2.11.0: google/resumable_media/_upload.py:505).
  - Initiate retries use `wait_and_retry` with the same RetryStrategy (google-resumable-media==2.11.0: google/resumable_media/requests/upload.py:401; google-resumable-media==2.11.0: google/resumable_media/requests/upload.py:410; google-resumable-media==2.11.0: google/resumable_media/requests/_request_helpers.py:116).
  - Chunk PUT: method PUT to the Location URL (google-resumable-media==2.11.0: google/resumable_media/_upload.py:57; google-resumable-media==2.11.0: google/resumable_media/_upload.py:577). Payload is prepared once per chunk and reused on retries (google-resumable-media==2.11.0: google/resumable_media/requests/upload.py:502). Content-Range: `bytes {start}-{end}/*` while the total is unknown (google-resumable-media==2.11.0: google/resumable_media/_upload.py:38; google-resumable-media==2.11.0: google/resumable_media/_upload.py:1524). When a read returns fewer bytes than chunk size, the total is set to end+1 and the final chunk is `bytes {start}-{end}/{total}` (google-resumable-media==2.11.0: google/resumable_media/_upload.py:1483; google-resumable-media==2.11.0: google/resumable_media/_upload.py:37; google-resumable-media==2.11.0: google/resumable_media/_upload.py:1528). An empty final range is `bytes */{total}` (google-resumable-media==2.11.0: google/resumable_media/_upload.py:39; google-resumable-media==2.11.0: google/resumable_media/_upload.py:1526). Chunk size must be a multiple of 256 KiB (google-resumable-media==2.11.0: google/resumable_media/_upload.py:370; google-resumable-media==2.11.0: google/resumable_media/common.py:26).
  - Chunk responses: 200 means finished (bytes_uploaded += sent; checksum validation skipped because the client passes no checksum) (google-resumable-media==2.11.0: google/resumable_media/_upload.py:700; google-resumable-media==2.11.0: google/resumable_media/_upload.py:735). 308 Resume Incomplete requires a `Range: bytes=0-N` header; bytes_uploaded = N+1 (google-resumable-media==2.11.0: google/resumable_media/_upload.py:686; google-resumable-media==2.11.0: google/resumable_media/_upload.py:709; google-resumable-media==2.11.0: google/resumable_media/_upload.py:46; google-resumable-media==2.11.0: google/resumable_media/_upload.py:722). A 308 without Range is an error in this path (google-resumable-media==2.11.0: google/resumable_media/_upload.py:707).
  - Retryable statuses for chunks: 408, 429, 500, 502, 503, 504, plus connection error classes (google-resumable-media==2.11.0: google/resumable_media/common.py:68; google-resumable-media==2.11.0: google/resumable_media/requests/_request_helpers.py:37; google-resumable-media==2.11.0: google/resumable_media/requests/_request_helpers.py:157). Budget: `max_retries`, initial delay 1.0, multiplier 2.0, max sleep 64 s (google-resumable-media==2.11.0: google/resumable_media/common.py:145; google-resumable-media==2.11.0: google/resumable_media/common.py:148; google-resumable-media==2.11.0: google/resumable_media/common.py:54; google-resumable-media==2.11.0: google/resumable_media/common.py:177).
  - Resume: resumable_media has `recover()`: PUT with `Content-Range: bytes */*` and no body, expects 308; `Range` gives the next byte, no Range means 0; then `stream.seek` (google-resumable-media==2.11.0: google/resumable_media/_upload.py:803; google-resumable-media==2.11.0: google/resumable_media/_upload.py:825; google-resumable-media==2.11.0: google/resumable_media/_upload.py:829; google-resumable-media==2.11.0: google/resumable_media/_upload.py:842; google-resumable-media==2.11.0: google/resumable_media/_upload.py:844; google-resumable-media==2.11.0: google/resumable_media/requests/upload.py:518).
  - BigQuery never calls `recover()`; it only calls `initiate` and `transmit_next_chunk`, and maps `resumable_media.InvalidResponse` to a BigQuery exception (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3163; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3227; google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2739). An interrupted upload after retries is therefore not resumed by the client (code reading).
- (e) n/a.


### dbt-bigquery direct calls (which endpoints dbt's own code calls)

Calls are to google-cloud-bigquery client methods. dbt never calls `query_and_wait`, `_get_query_results`, `list_rows`, or `default_job_creation_mode` (grep, both versions).

- dbt-bigquery==1.9.0:
  - tables.get: `get_table` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:184); (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:311); (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:522).
  - tables.delete: `delete_table` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:198).
  - tables.list: `list_tables` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:215); (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:277).
  - tables.patch: `update_table` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:633); (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:649).
  - datasets.get: `get_dataset` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:836); (dbt-bigquery==1.9.0: dbt/adapters/bigquery/impl.py:850).
  - datasets.list: `list_datasets` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:552).
  - datasets.insert: `create_dataset` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:539).
  - datasets.delete: `delete_dataset` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:528).
  - jobs.insert (via client.query, default api_method): (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:570).
  - jobs.get/getQueryResults (via query_job.result): (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:587); load result (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:497).
  - jobs.cancel: (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:143).
  - resumable load: `load_table_from_file` with no `size` (dbt-bigquery==1.9.0: dbt/adapters/bigquery/connections.py:495), so always the resumable path (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:2731).
  - Not called directly: jobs.query, routines.list, routines.get, datasets.patch (dbt calls `update_dataset` at impl.py:843, not in the brief's list), jobs.getQueryResults directly.
- dbt-bigquery==1.12.1:
  - tables.get: `get_table` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:368); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:600); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:298); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:826).
  - tables.delete: `delete_table` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:312).
  - tables.list: `list_tables` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:347); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:409).
  - tables.patch: `update_table` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:828); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:896).
  - routines.list: `list_routines` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:421). routines.get: `get_routine` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:462).
  - datasets.get: `get_dataset` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:1376); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/impl.py:1396).
  - datasets.list: `list_datasets` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:630).
  - datasets.insert: `create_dataset` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:617).
  - datasets.delete: `delete_dataset` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:606).
  - jobs.insert (via client.query with a stable job_id, job_retry=None): (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:663); (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:667).
  - jobs.get on 409 (attach to existing job): `client.get_job` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:251).
  - jobs.get reload inside the result() retry predicate (not for rate-limit errors): (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/retry.py:170).
  - jobs.getQueryResults/jobs.get via query_job.result: (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:684).
  - jobs.cancel: (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:144).
  - resumable load: `load_table_from_file` with no `size` (dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/connections.py:573).
  - Not called directly: jobs.query, routines (create), datasets.patch (dbt calls `update_dataset` at impl.py:1383, not in brief list).

### UNRESOLVED (BigQuery REST)

- UNRESOLVED: server default for `jobCreationMode` when the client does not send it. The package states only the three cases (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:19) and does not state the default.
- UNRESOLVED: whether the server can return `jobComplete=false` with no `jobReference`. The package assumes not (google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:591), but says nothing about the server.
- UNRESOLVED: the server's actual default `timeoutMs` for getQueryResults. The package only says "default of 10 seconds" in a comment (google-cloud-bigquery==3.46.1: google/cloud/bigquery/job/query.py:1525).
- UNRESOLVED: server meaning of 308 without a `Range` header. The transmit path treats it as an error (google-resumable-media==2.11.0: google/resumable_media/_upload.py:707) and recover treats it as 0 bytes (google-resumable-media==2.11.0: google/resumable_media/_upload.py:841); the server contract is not in the package.
- UNRESOLVED: whether a resumable session survives after `transmit_next_chunk` retries are exhausted. The package has `recover()` but BigQuery never calls it (google-cloud-bigquery==3.46.1: google/cloud/bigquery/client.py:3163); server session lifetime is not in the package.
- UNRESOLVED: server behaviour of 404 after 409 on jobs.insert (the package cites an issue link only, google-cloud-bigquery==3.46.1: google/cloud/bigquery/_job_helpers.py:170).
- UNRESOLVED: 3.27.0 `_initiate_resumable_upload` and `transmit` bodies were not compared line by line; only constants and call sites were grepped (google-cloud-bigquery==3.27.0: google/cloud/bigquery/client.py:2982).
- UNRESOLVED: 3.27.0 vs 3.46.1 differences for endpoints 5-12 beyond the listed grep tokens were not diffed exhaustively.
- UNRESOLVED: full JSON response schemas for each endpoint; the package reads only the fields listed above.


## Summary

| client | endpoint override | dummy credentials | result format to serve | blockers |
|---|---|---|---|---|
| google-cloud-bigquery (Python) | `BIGQUERY_EMULATOR_HOST` or `client_options={"api_endpoint": ...}` (both versions) | `AnonymousCredentials()` plus explicit `project` | REST JSON: `jobs.insert` by default, `getQueryResults` for rows | Storage Read API is gRPC; pass `create_bqstorage_client=False` or do not install the storage package. 3.46.1 Arrow is opt-in beta. |
| dbt-bigquery 1.9.0 | environment variable only (no profile `api_endpoint`) | `method: oauth-secrets` with a dummy `token` | REST JSON | `oauth` needs ADC, `service-account*` need a keyfile; the environment variable is process-wide. |
| dbt-bigquery 1.12.1 | profile `api_endpoint` | `method: oauth-secrets` with a dummy `token` | REST JSON | same as 1.9.0; adds `external-oauth-wif`. |
| snowflake-connector-python 3.12.0 | `host`, `port`, `protocol` (defaults `127.0.0.1:8080`, http) | any non-empty password, or an OAuth token; the login route must answer | JSON: send `queryResultFormat: "json"` | OCSP fail-open by default (`insecure_mode=True` skips it); session delete at close; `account` required; telemetry off unless the server enables it. |
| snowflake-connector-python 4.8.0 | `host`, `port`, `protocol` (defaults `127.0.0.1:443`, https) | same as 3.12.0; `AuthNoAuth` only via Python `auth_class` | JSON; the client re-fetches a short inline result from `/queries/{id}/result` | `account` required; session delete at close; telemetry needs the server flag. |
| dbt-snowflake 1.9.0 (connector 3.x) | profile `host`, `port`, `protocol` (set `protocol: http` and `port`, or `account` forces https:443) | `password: <any non-empty>` | JSON | `insecure_mode: true` for local OCSP skip. |
| dbt-snowflake 1.12.3 (connector 4.x) | profile `host`, `port`, `protocol` | same as 1.9.0 | JSON | OCSP disabled by default in the connector. |

Snowflake result format: the emulator should return `queryResultFormat: "json"` explicitly. The connector accepts it in both
versions, and the server default is `UNRESOLVED`.

Snowflake minimal config is in section 1 (Python) and the `profiles.yml` snippet in the dbt-snowflake section.

Snowflake `UNRESOLVED` items:
- Runtime behaviour over plain `http://` (both connector majors).
- Server-side password and token validation (performed by the server, not client code).
- The server default for `queryResultFormat` when the client sends no parameter.
- 3.12.0: whether a custom `AuthByKeyPair` subclass as `auth_class` can skip login.
- 4.8.0 `AuthNoAuth`: whether any HTTP calls happen after login is skipped was not traced.
- 4.8.0 CRL distribution-point hosts when CRL is enabled.
- dbt-snowflake 1.9.0 base-query citation is from dbt-adapters 1.24.5 (1.9.0 requires dbt-adapters>=1.10.4).
- dbt-snowflake 1.12.3 `platform_detection_timeout_seconds` connector endpoints.
- OCSP behaviour over `protocol=http`.

Not audited: the vendored urllib3 copy and the async variants of the connector.
