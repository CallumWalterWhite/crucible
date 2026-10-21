"""BigQuery AST transforms (docs/architecture.md, "Transforms").

Each area is one module exposing `transform(tree, ctx) -> tree`. `PIPELINE` is the only place that
decides the order. Transforms run on the qualified, type-annotated BigQuery AST (ADR 0010), before
the projection CASTs and DuckDB generation. A transform that rewrites an expression must keep or
set `.type` on what it builds, so later transforms still see types. A transform raises
`CrucibleUnsupportedError` for anything in its area it cannot translate exactly (ADR 0009).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlglot import exp

from crucible.dialects.bigquery.transforms import (
    arrays,
    dates,
    errors,
    json,
    literals,
    nan,
    regexp,
    safe,
    strings,
    unnest,
)
from crucible.engine import Session


@dataclass(frozen=True)
class TransformContext:
    session: Session


Transform = Callable[[exp.Expr, TransformContext], exp.Expr]

# Order matters: structure first (unnest), then NaN rules on the user's comparisons, then
# functions, then literal typing last so every literal a transform introduces is typed by
# BigQuery rules too. `safe` runs after `errors`
# because SAFE. turns the checks `errors` adds into NULLs.
PIPELINE: tuple[tuple[str, Transform], ...] = (
    ("unnest", unnest.transform),
    ("nan", nan.transform),
    ("json", json.transform),
    ("regexp", regexp.transform),
    ("strings", strings.transform),
    ("dates", dates.transform),
    ("arrays", arrays.transform),
    ("errors", errors.transform),
    ("safe", safe.transform),
    ("literals", literals.transform),
)


def apply(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    for _, transform in PIPELINE:
        tree = transform(tree, ctx)
    return tree
