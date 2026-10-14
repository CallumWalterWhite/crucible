"""BigQuery query translation: parse tree -> qualify -> annotate -> transforms -> casts -> DuckDB.

ADR 0010 (schema-aware translation) and ADR 0014 (output types come from the BigQuery AST).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from crucible.dialects.bigquery import errors, metadata, transforms, types
from crucible.dialects.bigquery.generator import generate
from crucible.engine import ExecContext, TableInfo

_NAME = "crucible_name"  # meta key: the output name as BigQuery reports it
_ANONYMOUS = ""


@dataclass(frozen=True)
class Translation:
    sql: str
    names: list[str]
    types: list[
        exp.DataType
    ]  # BigQuery types of the output columns (UNKNOWN where annotation failed)


def schema_for(infos: list[TableInfo]) -> MappingSchema:
    """A SQLGlot schema of the referenced tables, in BigQuery types (ADR 0010)."""
    mapping: dict[str, object] = {}
    for info in infos:
        columns = {c.name: types.type_name(c.type) for c in info.columns}
        datasets = cast(dict[str, dict[str, object]], mapping.setdefault(info.catalog, {}))
        datasets.setdefault(info.schema, {})[info.name] = columns
    return MappingSchema(mapping, dialect="bigquery")


def outer_selects(query: exp.Expr) -> list[exp.Select]:
    """The SELECTs whose projections are the result: one, or every branch of a set operation."""
    if isinstance(query, exp.SetOperation):
        return outer_selects(query.left) + outer_selects(query.right)
    if isinstance(query, exp.Subquery):
        return outer_selects(query.this)
    assert isinstance(query, exp.Select), f"not a query: {type(query).__name__}"
    return [query]


def _tag_names(select: exp.Select) -> None:
    """Record each projection's name as written, before qualify lowercases identifiers."""
    for projection in select.expressions:
        if isinstance(projection, exp.Alias):
            name = projection.args["alias"].this
        elif isinstance(projection, exp.Column) and not isinstance(projection.this, exp.Star):
            name = projection.this.this
        elif isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            continue  # expanded by qualify; names come from the table schema
        else:
            name = _ANONYMOUS
        projection.meta[_NAME] = name
        if isinstance(projection, exp.Alias):
            projection.this.meta[_NAME] = name


def _output_names(select: exp.Select, stored: dict[str, str]) -> list[str]:
    """BigQuery's output names: as written, stored names for SELECT *, f0_, f1_ ... if anonymous."""
    names: list[str] = []
    anonymous = 0
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        tag = projection.meta.get(_NAME, inner.meta.get(_NAME))
        if tag is None:  # from a star expansion
            name = stored.get(projection.alias_or_name.lower(), projection.alias_or_name)
        elif tag == _ANONYMOUS:
            # BigQuery names anonymous result columns f0_, f1_, ... in order.
            name = f"f{anonymous}_"
            anonymous += 1
        else:
            name = str(tag)
        names.append(name)
    return names


def _qualify_error(exc: OptimizeError) -> Exception:
    text = str(exc)
    column = re.search(r"Column '(.+?)' could not be resolved", text)
    if column:
        return errors.invalid(f"Unrecognized name: {column.group(1).strip(chr(34) + '`')}")
    return errors.invalid(text)


def translate_query(
    query: exp.Expr,
    ctx: ExecContext,
    target_types: list[exp.DataType] | None = None,
) -> Translation:
    """Translate a query. `target_types` (INSERT columns) replace the annotated output types."""
    first = outer_selects(query)[0]
    _tag_names(first)
    infos = metadata.qualify_names(query, ctx)
    schema = schema_for(infos)
    try:
        query = qualify(query, schema=schema, dialect="bigquery", validate_qualify_columns=True)
    except OptimizeError as exc:
        raise _qualify_error(exc) from exc
    query = annotate_types(query, schema=schema, dialect="bigquery")
    fix_annotation(query)

    selects = outer_selects(query)
    stored = {c.name.lower(): c.name for info in infos for c in info.columns}
    names = _output_names(selects[0], stored)
    out_types = target_types or _output_types(selects)

    query = transforms.apply(query, transforms.TransformContext(ctx.session))
    _storage_casts(query)
    for select in outer_selects(query):
        if len(select.expressions) != len(names):
            raise errors.invalid("Queries in a set operation have mismatched column counts")
        _cast_projections(select, names, out_types)
    return Translation(generate(query), names, out_types)


_UNKNOWN = exp.DataType.build("UNKNOWN")


def _value(projection: exp.Expr) -> exp.Expr:
    return projection.this if isinstance(projection, exp.Alias) else projection


def _output_types(selects: list[exp.Select]) -> list[exp.DataType]:
    """Each output column's BigQuery type; for a set operation, the branches' supertype."""
    result: list[exp.DataType] = []
    for index, projection in enumerate(selects[0].expressions):
        column_type = _value(projection).type or _UNKNOWN
        for select in selects[1:]:
            other = _value(select.expressions[index]).type or _UNKNOWN
            if _UNKNOWN.this in (column_type.this, other.this):
                column_type = _UNKNOWN
                break
            common = types.supertype(column_type, other)
            if common is None:
                raise errors.invalid(
                    f"Column {index + 1} in set operation has incompatible types: "
                    f"{types.type_name(column_type)}, {types.type_name(other)}"
                )
            column_type = common
        result.append(column_type)
    return result


def fix_annotation(tree: exp.Expr) -> None:
    """BigQuery typing rules SQLGlot 30.22 gets wrong. A wrong type is worse than UNKNOWN: the
    projection CAST would convert the value (ADR 0014). Each rule has a unit test."""
    # One spelling per BigQuery type: the annotator types integer literals INT, and so on.
    for node in tree.walk():
        if node.type is not None:
            node.type = types.normalize(node.type)
    # `a || b` on arrays is ARRAY_CONCAT and returns the array type; SQLGlot types it STRING.
    # Innermost first, so a chain `a || b || c` sees its left operand already re-typed.
    for node in reversed(list(tree.find_all(exp.DPipe))):
        for operand in (node.left, node.right):
            if operand.type is not None and operand.type.this == exp.DataType.Type.ARRAY:
                node.type = operand.type.copy()
                break


def _storage_casts(tree: exp.Expr) -> None:
    """Every CAST target becomes its storage type: NUMERIC is DECIMAL(38,9), not DuckDB's
    DECIMAL(18,3) (ADR 0014). BIGNUMERIC and other unsupported targets raise here."""
    for node in tree.find_all(exp.Cast):
        target = node.args.get("to")
        if isinstance(target, exp.DataType) and not node.meta.get("crucible_storage"):
            node.set("to", types.storage_type(target))
            node.meta["crucible_storage"] = True


def _cast_projections(select: exp.Select, names: list[str], out_types: list[exp.DataType]) -> None:
    """CAST each projection to its BigQuery type's storage type (ADR 0014, "Casts").

    The CAST makes DuckDB's result type the storage type exactly, and is BigQuery's overflow check
    (HUGEINT -> BIGINT raises). Projections annotation could not type keep DuckDB's own type.
    """
    projections: list[exp.Expr] = []
    for projection, name, out_type in zip(select.expressions, names, out_types, strict=True):
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if out_type.this != exp.DataType.Type.UNKNOWN:
            value = exp.Cast(this=value, to=types.storage_type(out_type))
        projections.append(exp.alias_(value, name, quoted=True))
    select.set("expressions", projections)
