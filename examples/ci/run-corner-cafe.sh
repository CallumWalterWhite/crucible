#!/usr/bin/env sh
# dbt seed, run and test for examples/dbt-bigquery/corner_cafe against a Crucible already listening
# at $CRUCIBLE_URL. The GitHub Actions recipe (examples/ci/github-actions.yml) and the dbt-example job
# in .github/workflows/ci.yml both call this script, so the dbt steps have one source.
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
venv="${DBT_VENV:-$(mktemp -d)/venv}"
uv venv --quiet --python 3.12 "$venv"
uv pip install --quiet --python "$venv/bin/python" -r "$root/examples/dbt-bigquery/requirements.txt"

export DBT_SEND_ANONYMOUS_USAGE_STATS=false
cd "$root/examples/dbt-bigquery/corner_cafe"
"$venv/bin/dbt" seed --profiles-dir .
"$venv/bin/dbt" run --profiles-dir .
"$venv/bin/dbt" test --profiles-dir .
