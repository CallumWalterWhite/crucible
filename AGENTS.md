# AGENTS.md

Local BigQuery/Snowflake API emulator on DuckDB. Pre-alpha.

## Commands (use `uv run`)

Setup: `uv sync --all-extras` then `uv run pre-commit install`.

Pre-PR checks (same as CI `lint` job, in this order):
```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run lint-imports
uv run pytest
```
Default `pytest` runs only `tests/unit` + `tests/corpus`. Single test: `uv run pytest <path> [-k name]`. Single corpus case without a server: `uv run crucible corpus run --dialect bigquery --path corpus/bigquery/<area>/<case>.yaml`.

`clients`/`dbt` suites need a running Docker image + `CRUCIBLE_URL` (see `.github/workflows/ci.yml`); don't run them locally by default. `serve` needs the `server` extra (`--all-extras` covers it).

## Architecture

Layers enforced by `import-linter` (`pyproject.toml`): `crucible.cli` → `crucible.server | crucible.corpus` → `crucible.dialects` → `crucible.engine`. `bigquery` and `snowflake` dialect packages never import each other; engine is warehouse-agnostic and sees dialects only via the `Dialect` protocol (type-checking only). Request flow: `server/<dialect>` → `engine.Pipeline` (`dialect.split` → `dialect.plan` → `SqlPlan | CommandPlan`) → `Result` → dialect types → wire response.

- `engine/`: `engine.py` (one DuckDB, UTC-pinned, cursor per request), `catalog.py` (quote all identifiers via `engine.catalog.quote()`), `session.py`, `pipeline.py`, `types.py`, `errors.py`.
- `dialects/<d>/`: `statements.py`, `translate.py` (schema-aware: qualify against live catalog, annotate, transform, generate with the dialect's own SQLGlot DuckDB-generator subclass), `ddl.py`, `dml.py`, `types.py`, `metadata.py`, `errors.py`, `dialect.py`.
- `dialects/<d>/transforms/`: one module per area; ordered list `PIPELINE` in `transforms/__init__.py` is the only ordering. Transforms run on the qualified type-annotated AST, must preserve `.type` on rebuilt nodes, and raise `CrucibleUnsupportedError` for anything inexact.
- `server/`: one Starlette app per dialect per process. BigQuery `:9050`, Snowflake `:8080`, bind `127.0.0.1` by default (`CRUCIBLE_HOST=0.0.0.0` only inside Docker). Health: `GET /_crucible/health`. Unknown endpoints return the warehouse's "not implemented" shape, never a generic 404. Never expose on a network — no auth by design.

## Rules that fail CI or waste money

- **Unsupported means explicit.** Never return a best guess; raise `CrucibleUnsupportedError` (mapped to the warehouse error at the server edge). `docs/architecture.md` + ADRs are design truth; `CONTRIBUTING.md` has the workflow.
- **Corpus is the proof.** Cases live in `corpus/<dialect>/<area>/<case>.yaml`. Never hand-set `verified: true` or `recorded_at` — only `crucible corpus record` writes them, and CI (`scripts/check-corpus-verified.sh`) fails any PR outside `corpus-record/*` that adds `verified: true`. Contributors never run `record`/`record-api` (spends real warehouse credits, requires `--max-bytes-billed` + `--i-understand-this-spends-credits`, maintainers only).
- **Every response shape needs a one-line comment** citing the client source or official API doc. Upstream SQLGlot bugs go upstream; keep only a local workaround with a comment linking the upstream PR.
- **Exact pins** `sqlglot==30.22.0`, `duckdb==1.5.6` are deliberate (ADR 0008) — do not loosen. `pytz` is a core dep (DuckDB needs it for TZ values). Python 3.11–3.13; `pyright` is `strict` on `src` only; ruff line-length 100.
- **`docs/compatibility.md` is generated** from corpus/client results — never hand-edit. Wire fixtures in `tests/fixtures/bigquery_api/` are redacted in code before writing.
