"""STRUCT literals inside UNNEST, built as typed ROWs.

DuckDB 1.5.6 loses the field names of a struct literal (`{'a': ...}`) inside UNNEST when a field
calls a DuckDB macro (NULLIF, ARRAY_REVERSE, ...) on a correlated column, and fails with "Binder
Error: Need named argument for struct pack". `CAST(ROW(...) AS STRUCT(...))` names the fields
through the type instead, so it binds. The field types come from BigQuery's annotation (an array
element's type when the struct is in an array literal, so an unnamed STRUCT('b', 1) takes the
array's field names, as in BigQuery).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery import types
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    for struct in list(tree.find_all(exp.Struct)):
        # Only a column reference can be correlated, so constant structs bind as they are. The
        # other structs of the same array literal are rebuilt too: DuckDB matches struct fields by
        # name when it unifies the elements, and an unnamed literal's fields would read as NULL.
        scope = struct.parent if isinstance(struct.parent, exp.Array) else struct
        if struct.find_ancestor(exp.Unnest) is None or scope.find(exp.Column) is None:
            continue
        target = struct.type
        parent = struct.parent
        if isinstance(parent, exp.Array) and parent.type and parent.type.this == exp.DType.ARRAY:
            target = parent.type.expressions[0] if parent.type.expressions else target
        if target is None or target.this != exp.DType.STRUCT:
            continue
        try:
            storage = types.storage_type(target)
        except CrucibleUnsupportedError:
            continue  # a field Crucible cannot type; translation refuses it elsewhere
        values = [_value(field) for field in struct.expressions]
        if len(values) != len(storage.expressions) or not all(c.name for c in storage.expressions):
            continue  # unnamed fields have no names to lose
        typed = exp.Cast(this=exp.Anonymous(this="ROW", expressions=values), to=storage)
        typed.type = target
        struct.replace(typed)
    return tree


def _value(field: exp.Expr) -> exp.Expr:
    if isinstance(field, exp.PropertyEQ):
        return field.expression
    if isinstance(field, exp.Alias):
        return field.this
    return field
