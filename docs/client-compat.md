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
  `gcloud auth application-default login`. (dbt-bigquery==1.9.0: dbt/adapters/bigquery/clients.py:22-29,
  dbt/adapters/bigquery/credentials.py:159-172; dbt-bigquery==1.12.1: src/dbt/adapters/bigquery/clients.py:32-39,
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
