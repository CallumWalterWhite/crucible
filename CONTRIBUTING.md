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

## Licensing

Crucible is licensed under the Apache License 2.0. By contributing, you agree that your contributions are licensed under
the Apache License 2.0. No Developer Certificate of Origin sign-off is required.
