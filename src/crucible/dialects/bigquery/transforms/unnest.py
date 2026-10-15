"""UNNEST as a relation (in FROM or JOIN).

BigQuery behaviours DuckDB's UNNEST does not have:
- `WITH OFFSET o` numbers elements from 0; DuckDB's WITH ORDINALITY numbers from 1.
- An unaliased UNNEST of an ARRAY<STRUCT> exposes the struct's fields as columns; DuckDB exposes
  one struct column.

Both are rewritten to a lateral subquery whose columns are the names qualify already gave the
UNNEST's table alias, so references elsewhere in the query stay valid:

    (SELECT crucible_u.v AS x, crucible_u.i - 1 AS o
     FROM (SELECT UNNEST(list_transform(<arr>, (e, i) -> struct_pack(v := e, i := i)))
           AS crucible_u))

A LEFT JOIN keeps its NULL row for an empty array (the join condition becomes ON TRUE).
Name resolution between an UNNEST alias and a joined column needs no rewrite: qualify resolves it
by BigQuery's rules and every generated identifier is qualified and quoted (ADR 0010).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import T

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

_ROW = "crucible_u"


def _struct_fields(element_type: exp.DataType | None) -> list[str]:
    if element_type is None or element_type.this != T.STRUCT:
        return []
    return [c.name for c in element_type.expressions if isinstance(c, exp.ColumnDef)]


def _field(source: exp.Expr, name: str) -> exp.Expr:
    return exp.Anonymous(this="struct_extract", expressions=[source, exp.Literal.string(name)])


def _rewrite(node: exp.Unnest) -> exp.Expr | None:
    alias = node.args.get("alias")
    offset = node.args.get("offset")
    if not isinstance(alias, exp.TableAlias) or len(node.expressions) != 1:
        return None
    [array] = node.expressions
    element_type = None
    if array.type is not None and array.type.this == T.ARRAY and array.type.expressions:
        inner = array.type.expressions[0]
        element_type = inner if isinstance(inner, exp.DataType) else None
    columns = [c.name for c in alias.columns]
    fields = _struct_fields(element_type) if not columns else []
    if not isinstance(offset, exp.Identifier) and not fields:
        return None  # DuckDB's UNNEST already behaves like BigQuery's here

    row = exp.column(_ROW)
    value = _field(row.copy(), "v")
    if fields:
        projections: list[exp.Expr] = [
            exp.alias_(_field(value.copy(), f), f, quoted=True) for f in fields
        ]
    elif columns:
        projections = [exp.alias_(value, columns[0], quoted=True)]
    else:
        return None
    if isinstance(offset, exp.Identifier):
        index = exp.Sub(this=_field(row.copy(), "i"), expression=exp.Literal.number(1))
        projections.append(exp.alias_(index, offset.name, quoted=True))

    pairs = exp.Lambda(
        this=exp.Anonymous(
            this="struct_pack",
            expressions=[
                exp.PropertyEQ(this=exp.to_identifier("v"), expression=exp.column("e")),
                exp.PropertyEQ(this=exp.to_identifier("i"), expression=exp.column("i")),
            ],
        ),
        expressions=[exp.to_identifier("e"), exp.to_identifier("i")],
    )
    listed = exp.Anonymous(this="list_transform", expressions=[array.copy(), pairs])
    rows = exp.select(exp.alias_(exp.Unnest(expressions=[listed]), _ROW, quoted=True)).subquery()
    query = exp.select(*projections).from_(rows)
    return exp.Subquery(this=query, alias=exp.TableAlias(this=alias.this.copy()))


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    for node in list(tree.find_all(exp.Unnest)):
        parent = node.parent
        if not isinstance(parent, exp.From | exp.Join):
            continue
        replacement = _rewrite(node)
        if replacement is None:
            continue
        node.replace(replacement)
        if isinstance(parent, exp.Join) and parent.side == "LEFT" and not parent.args.get("on"):
            parent.set("on", exp.true())
    return tree
