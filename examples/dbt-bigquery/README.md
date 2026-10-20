# dbt BigQuery example

A small dbt project (`corner_cafe`: four seeds, four staging views, three mart tables, data tests and two
unit tests) that runs against Crucible with dbt-core 1.12 and the standard dbt BigQuery adapter. Its data is
invented.

From the repository root, start Crucible in one terminal:

```sh
uv sync --all-extras
uv run crucible serve --dialect bigquery
```

(or the Docker image from the main README). In another terminal, also from the repository root:

```sh
uv venv examples/dbt-bigquery/.venv --python 3.12
uv pip install --python examples/dbt-bigquery/.venv/bin/python \
  -r examples/dbt-bigquery/requirements.txt
cd examples/dbt-bigquery/corner_cafe
export DBT_SEND_ANONYMOUS_USAGE_STATS=false
../.venv/bin/dbt seed --profiles-dir .
../.venv/bin/dbt run --profiles-dir .
../.venv/bin/dbt test --profiles-dir .
```

The commands call the virtualenv's dbt-core by path. A plain `dbt` may be dbt Fusion (2.x), which installs
itself on `PATH` and cannot run the tests (see Known limits). `../.venv/bin/dbt --version` should report
Core 1.12.6.

Expected: `dbt seed` loads 4 seeds, `dbt run` builds 7 models (4 views, 3 tables), and `dbt test` passes 22
tests (19 generic, 1 singular, 2 unit).

Always pass `--profiles-dir .` to use the included `crucible` target instead of your personal dbt profile. It
uses project `crucible-example`, dataset `corner_cafe` (seeds in `corner_cafe_raw`), four threads, and a
dummy token. The adapter is pinned to a version that supports the profile's `api_endpoint`.

The default endpoint is `http://127.0.0.1:9050`. For another local Crucible port, set `CRUCIBLE_URL`, for
example `export CRUCIBLE_URL=http://127.0.0.1:19050`. The server stores data in memory by default; rerun the
seeds and models after restarting it.

## Known limits

- **Seed timestamps are typed as TIMESTAMP** in `dbt_project.yml`. dbt types a timestamp-looking seed column
  as DATETIME, and Crucible's CSV loader raises `Not supported by Crucible: CSV load into a DATETIME column`
  because BigQuery's DATETIME load spellings are not recorded yet. Boolean columns spelled `True`/`False`
  (what Python tools write) load as BOOL.
- **dbt Fusion (the `dbt` 2.x binary)** can run `dbt seed` and `dbt run` against Crucible, but not `dbt test`
  or unit tests: Fusion reads query results through the BigQuery Storage Read API, which Crucible does not
  implement. Use dbt-core from the virtualenv, as above. Details: [docs/client-compat.md](../../docs/client-compat.md).
