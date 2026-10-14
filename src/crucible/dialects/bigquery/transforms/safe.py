"""SAFE. prefix and SAFE_* functions: suppress runtime errors, return NULL (ADR 0011). Slice 2."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    return tree
