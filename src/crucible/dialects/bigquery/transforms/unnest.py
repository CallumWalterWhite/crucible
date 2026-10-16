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
from sqlglot.helper import find_new_name

from crucible.dialects.bigquery.transforms.common import T
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

_ROW = "crucible_u"
_SYNTHETIC = "crucible_synthetic_alias"  # meta key: alias added by alias_offset_unnests


def alias_offset_unnests(query: exp.Expr) -> set[str]:
    """Name an unaliased UNNEST ... WITH OFFSET before qualify (called from prepare).

    BigQuery names the value column after the array, so UNNEST(t.arr) yields arr. Without a
    name qualify drops the value from SELECT *, and the offset silently displaces it. The
    table alias is synthetic (a real name would collide with the table's own columns).
    Only the value column takes the array's name. BigQuery reports a bare reference to
    the array name as ambiguous; here it resolves to the unnested value. Scalar array
    expressions get an internal value name, returned here so output naming can report
    them as anonymous columns (f0_, f1_, ...).
    """
    count = 0
    taken = {identifier.name.lower() for identifier in query.find_all(exp.Identifier)}
    anonymous: set[str] = set()
    for node in query.find_all(exp.Unnest):
        # sqlglot parses a missing WITH OFFSET as False; only a real offset needs a name.
        if node.args.get("alias") is not None or not isinstance(
            node.args.get("offset"), exp.Identifier
        ):
            continue
        if len(node.expressions) != 1:
            continue
        table_name = find_new_name(taken, f"crucible_unnest_{count}")
        taken.add(table_name)
        [array] = node.expressions
        if isinstance(array, exp.Column):
            name = array.name
        else:
            name = find_new_name(taken, f"{table_name}_value")
            taken.add(name)
            anonymous.add(name)
        alias = exp.TableAlias(
            this=exp.to_identifier(table_name),
            columns=[exp.to_identifier(name)],
        )
        alias.meta[_SYNTHETIC] = True
        node.set("alias", alias)
        count += 1
    return anonymous


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
    if alias.meta.get(_SYNTHETIC) and element_type is not None and element_type.this == T.STRUCT:
        # A synthetic alias on a STRUCT array: BigQuery exposes the fields alongside the
        # offset, but qualify already fixed the outer SELECT to a single value column.
        raise CrucibleUnsupportedError("UNNEST WITH OFFSET over an array of STRUCTs")
    if isinstance(offset, exp.Identifier) and not columns:
        if fields:
            # A struct literal: aliasing would keep one STRUCT value, but BigQuery
            # exposes the fields, so an alias would not fix this either.
            raise CrucibleUnsupportedError("UNNEST WITH OFFSET over an array of STRUCTs")
        # Refuse any remaining shape without a value column rather than lose it from SELECT *.
        raise CrucibleUnsupportedError("UNNEST WITH OFFSET without a table alias")
    if not isinstance(offset, exp.Identifier) and not fields:
        return None  # DuckDB's UNNEST already behaves like BigQuery's here

    row = exp.column(_ROW)
    value = _field(row.copy(), "v")
    if fields:
        projections: list[exp.Expr] = [
            exp.alias_(_field(value.copy(), f), f, quoted=True) for f in fields
        ]
    else:
        projections = [exp.alias_(value, columns[0], quoted=True)]
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
