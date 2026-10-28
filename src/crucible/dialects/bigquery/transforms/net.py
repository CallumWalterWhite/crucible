"""NET functions: NET.REG_DOMAIN runs as Crucible's Python function (dialects/bigquery/net.py).
The other NET functions are refused."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery import net
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    for node in list(tree.find_all(exp.NetFunc)):
        inner = node.this
        if not isinstance(inner, exp.RegDomain):
            name = inner.sql_name() if isinstance(inner, exp.Func) else inner.sql("bigquery")
            raise CrucibleUnsupportedError(f"function NET.{name}")
        call = exp.Anonymous(this=net.FUNCTION, expressions=[inner.this])
        call.type = exp.DataType.build("STRING", dialect="bigquery")
        node.replace(call)
    return tree
