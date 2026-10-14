"""Dates and times: DATE results of DATE_TRUNC/DATE_ADD/DATE_SUB, TIMESTAMP_DIFF. Slice 2."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    return tree
