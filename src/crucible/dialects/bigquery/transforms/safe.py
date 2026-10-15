"""The SAFE. prefix (ADR 0011): an error raised by the function itself becomes NULL.

BigQuery suppresses only the function's own error, not an error from evaluating its arguments
(`SAFE.LOG(1 / 0)` still raises division by zero). DuckDB's TRY(expr) suppresses every error inside
`expr`, arguments included, so an argument that might raise is evaluated outside the TRY:

    list_extract(list_transform([struct_pack(a0 := <arg>)], crucible_s -> TRY(f(crucible_s.a0))), 1)

Arguments that cannot raise (columns, literals, NULL) stay inline: TRY(f(col)).
The checks the `errors` transform adds inside f (e.g. for SAFE.CAST) are inside the TRY, so they
turn into NULL as BigQuery's SAFE versions do.
"""

from __future__ import annotations

from itertools import count
from typing import TYPE_CHECKING, cast

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import rewrite_bottom_up, typed
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

_INERT = (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Identifier, exp.Var, exp.DataType)


def _inert(node: exp.Expr) -> bool:
    if isinstance(node, exp.Neg):
        return _inert(node.this)
    return isinstance(node, _INERT)


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    names = count()

    def rewrite(node: exp.Expr) -> exp.Expr:
        if not isinstance(node, exp.SafeFunc):
            return node
        func = node.this
        if isinstance(func, exp.AggFunc):
            raise CrucibleUnsupportedError(f"SAFE. on an aggregate: {node.sql('bigquery')}")
        var = f"crucible_s{next(names)}"
        lifted: list[exp.Expr] = []
        for key, value in list(func.args.items()):
            values = cast(list[object], value) if isinstance(value, list) else [cast(object, value)]
            replaced: list[object] = []
            for item in values:
                if isinstance(item, exp.Expr) and not _inert(item):
                    field = f"a{len(lifted)}"
                    lifted.append(exp.PropertyEQ(this=exp.to_identifier(field), expression=item))
                    replaced.append(exp.column(field, table=var))
                else:
                    replaced.append(item)
            func.set(key, replaced if isinstance(value, list) else replaced[0])
        guarded = exp.Anonymous(this="try", expressions=[func])
        if lifted:
            row = exp.Anonymous(this="struct_pack", expressions=lifted)
            lam = exp.Lambda(this=guarded, expressions=[exp.to_identifier(var)])
            each = exp.Anonymous(
                this="list_transform", expressions=[exp.Array(expressions=[row]), lam]
            )
            guarded = exp.Anonymous(this="list_extract", expressions=[each, exp.Literal.number(1)])
        if func.type is not None:
            typed(guarded, func.type)
        return guarded

    return rewrite_bottom_up(tree, rewrite)
