# Contributing

Thanks for your interest in Crucible. The project is pre-alpha, so expect the layout and the APIs to change.

## Setup

```sh
uv sync --all-extras
uv run pre-commit install
```

## Checks

Run these before you open a pull request. CI runs the same checks.

```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run lint-imports
uv run pytest
```

## Principles

- **Correctness over performance.** A slow, correct answer is better than a fast, wrong one.
- **Unsupported means explicit.** Anything Crucible does not support raises `CrucibleUnsupportedError`. It must never
  return a silently wrong result.
- **Fidelity is measured by the corpus.** Claims about what Crucible matches are backed by recorded cases, not by
  reasoning alone.

## Corpus rules

Test cases live in `corpus/<dialect>/<area>/<case>.yaml`.

- `verified: true` and `recorded_at` are written **only** by `crucible corpus record`. Never set them by hand.
- Maintainers run `crucible corpus record`. It runs each case against a real warehouse and spends warehouse credits, so
  contributors do not run it.
- Every response shape must cite the client source or the official API documentation in a one-line comment next to it.

## Upstream fixes

If a fix belongs in SQLGlot, send it upstream. In Crucible, keep only a local workaround, and link the upstream pull
request from a comment next to the workaround.

## Design

Read [docs/architecture.md](docs/architecture.md) for the design, and [docs/adr/](docs/adr/) for the decisions behind it.

## Publishing (maintainers)

The Release workflow runs the full CI workflow before publishing Docker images for amd64 and arm64.
To check a release without publishing, run it manually with `dry_run: true`.

For a new release, update the package version with `uv version --no-sync <version>`, update the
changelog, and commit both `pyproject.toml` and `uv.lock`. Push a matching tag, for example
`v0.1.0a1` for package version `0.1.0a1`. The workflow publishes that version and `latest` to
`ghcr.io/callumwalterwhite/crucible-emulator`. On first publication, set the package visibility
to public in GitHub Packages so anonymous pulls work.

PyPI publishing is disabled by default. To enable it, configure a PyPI Trusted Publisher for
`CallumWalterWhite/crucible`, workflow `release.yml`, environment `pypi`, then set the repository
variable `PYPI_PUBLISHING_ENABLED` to `true`. A release dry run never publishes to either registry.

## Licensing

Crucible is licensed under the Apache License 2.0. By contributing, you agree that your contributions are licensed under
the Apache License 2.0. No Developer Certificate of Origin sign-off is required.
