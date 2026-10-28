"""BigQuery query translation: parse tree -> qualify -> annotate -> transforms -> casts -> DuckDB.

ADR 0010 (schema-aware translation) and ADR 0014 (output types come from the BigQuery AST).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from crucible.dialects.bigquery import (
    errors,
    information_schema,
    metadata,
    parameters,
    scripting,
    transforms,
    types,
    udfs,
)
from crucible.dialects.bigquery.generator import generate
from crucible.dialects.bigquery.transforms.common import check, raise_runtime
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


def _struct_field_names(query: exp.Expr) -> list[tuple[exp.Identifier, str]]:
    """STRUCT field names as written: in STRUCT(...) values and STRUCT<...> types. qualify
    lowercases them; BigQuery keeps their case in results and in TO_JSON_STRING keys."""
    found: list[tuple[exp.Identifier, str]] = []
    for struct in query.find_all(exp.Struct):
        for field in struct.expressions:
            if isinstance(field, exp.PropertyEQ) and isinstance(field.this, exp.Identifier):
                found.append((field.this, field.this.name))
            elif isinstance(field, exp.Alias) and isinstance(
                field.args.get("alias"), exp.Identifier
            ):
                found.append((field.args["alias"], field.alias))
    for data_type in query.find_all(exp.DataType):
        if data_type.this == exp.DType.STRUCT:
            for field in data_type.expressions:
                if isinstance(field, exp.ColumnDef) and isinstance(field.this, exp.Identifier):
                    found.append((field.this, field.name))
    return found


def _restore_struct_field_names(found: list[tuple[exp.Identifier, str]]) -> None:
    for identifier, name in found:
        identifier.set("this", name)


def _output_names(
    select: exp.Select, stored: dict[str, str], anonymous_columns: set[str]
) -> list[str]:
    """BigQuery's output names: as written, stored names for SELECT *, f0_, f1_ ... if anonymous."""
    names: list[str] = []
    anonymous = 0
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        tag = projection.meta.get(_NAME, inner.meta.get(_NAME))
        if tag is None and projection.alias_or_name in anonymous_columns:
            tag = _ANONYMOUS
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


@dataclass(frozen=True)
class Prepared:
    """A query after qualify, annotation and transforms: BigQuery semantics, DuckDB-ready AST."""

    query: exp.Expr
    names: list[str]
    types: list[exp.DataType]


def prepare(query: exp.Expr, ctx: ExecContext) -> Prepared:
    """Everything except the projection casts and generation (ADR 0010)."""
    information_schema.expand(query, ctx)
    parameters.bind(query, ctx)
    anonymous_columns = transforms.unnest.alias_offset_unnests(query)
    first = outer_selects(query)[0]
    _tag_names(first)
    infos = metadata.qualify_names(query, ctx)
    scripting.bind(query, ctx, {c.name.lower() for info in infos for c in info.columns})
    schema = schema_for(infos)
    struct_names = _struct_field_names(query)
    try:
        query = qualify(query, schema=schema, dialect="bigquery", validate_qualify_columns=True)
    except OptimizeError as exc:
        raise _qualify_error(exc) from exc
    _restore_struct_field_names(struct_names)
    query = annotate_types(query, schema=schema, dialect="bigquery")
    fix_annotation(query)
    udfs.check(query)

    selects = outer_selects(query)
    if any(len(select.expressions) != len(selects[0].expressions) for select in selects[1:]):
        raise errors.invalid("Queries in a set operation have mismatched column counts")
    stored = {c.name.lower(): c.name for info in infos for c in info.columns}
    names = _output_names(selects[0], stored, anonymous_columns)
    out_types = _output_types(selects)

    query = transforms.apply(query, transforms.TransformContext(ctx.session))
    _storage_casts(query)
    return Prepared(query, names, out_types)


def translate_query(
    query: exp.Expr,
    ctx: ExecContext,
    target_types: list[exp.DataType] | None = None,
) -> Translation:
    """Translate a query. `target_types` (INSERT columns) replace the annotated output types."""
    prepared = prepare(query, ctx)
    out_types = target_types or prepared.types
    for select in outer_selects(prepared.query):
        _cast_projections(select, prepared.names, out_types)
    return Translation(generate(prepared.query), prepared.names, out_types)


def write_value(value: exp.Expr, warehouse_type: exp.DataType, field: str) -> exp.Expr:
    """A value as BigQuery writes it into a column of `warehouse_type` (finalised arrays, cast)."""
    out = exp.Cast(
        this=finalize_value(value, warehouse_type, field, iter(range(1_000_000))),
        to=types.storage_type(warehouse_type),
    )
    out.meta["crucible_storage"] = True
    return out


_UNKNOWN = exp.DataType.build("UNKNOWN")
_NULL = exp.DataType(this=exp.DType.NULL)


def _value(projection: exp.Expr) -> exp.Expr:
    return projection.this if isinstance(projection, exp.Alias) else projection


def _output_types(selects: list[exp.Select]) -> list[exp.DataType]:
    """Each output column's BigQuery type; for a set operation, the branches' supertype."""
    result: list[exp.DataType] = []

    def type_of(projection: exp.Expr) -> exp.DataType:
        value = _value(projection)
        # A literal NULL takes the other branches' type (SQLGlot types a bare NULL INT64).
        if isinstance(value, exp.Null):
            return _NULL
        return value.type or _UNKNOWN

    for index, projection in enumerate(selects[0].expressions):
        column_type = type_of(projection)
        for select in selects[1:]:
            other = type_of(select.expressions[index])
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
    # UNNEST ... WITH OFFSET o: the offset is INT64; SQLGlot types it like the element.
    offsets = {
        (unnest.args["alias"].name, unnest.args["offset"].name)
        for unnest in tree.find_all(exp.Unnest)
        if isinstance(unnest.args.get("offset"), exp.Identifier)
        and isinstance(unnest.args.get("alias"), exp.TableAlias)
    }
    for column in tree.find_all(exp.Column):
        if (column.table, column.name) in offsets:
            column.type = exp.DataType.build("BIGINT")
    # `a || b` on arrays is ARRAY_CONCAT and returns the array type, and on BYTES returns BYTES;
    # SQLGlot types both STRING. Innermost first, so a chain `a || b || c` sees its left operand
    # already re-typed.
    for node in reversed(list(tree.find_all(exp.DPipe))):
        for operand in (node.left, node.right):
            if operand.type is not None and operand.type.this == exp.DataType.Type.ARRAY:
                node.type = operand.type.copy()
                break
        binary = (exp.DType.BINARY, exp.DType.VARBINARY, exp.DType.BLOB)
        if all(o.is_type(*binary) for o in (node.left, node.right)):
            node.type = exp.DataType.build("BYTES", dialect="bigquery")


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
    Arrays are finalised first (`finalize_value`).
    """
    projections: list[exp.Expr] = []
    for projection, name, out_type in zip(select.expressions, names, out_types, strict=True):
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(value, exp.Subquery) and value.args.get("alias"):
            # qualify names an unaliased scalar subquery on the node itself ((...) AS _col_0);
            # inside a CAST that alias is invalid SQL, and the output name is set below.
            value = value.copy()
            value.set("alias", None)
        if out_type.this != exp.DataType.Type.UNKNOWN:
            value = finalize_value(value, out_type, name, iter(range(1_000_000)))
            value = exp.Cast(this=value, to=types.storage_type(out_type))
            value.meta["crucible_storage"] = True
        projections.append(exp.alias_(value, name, quoted=True))
    select.set("expressions", projections)


def _has_array(data_type: exp.DataType) -> bool:
    return any(t.this == exp.DType.ARRAY for t in data_type.find_all(exp.DataType))


def finalize_value(
    value: exp.Expr, data_type: exp.DataType, field: str, names: Iterator[int]
) -> exp.Expr:
    """BigQuery's rules for arrays in a written result (query results, CTAS, INSERT):

    - an array with a NULL element raises "Array cannot have a null element; error in writing
      field <field>" (recorded);
    - a NULL array is written as an empty array (recorded: ARRAY_AGG over no rows is []).

    Inside the query arrays keep NULLs and may be NULL; only the written value changes. Applied
    recursively through STRUCT fields and array elements.
    """
    if not _has_array(data_type):
        return value
    if data_type.this == exp.DType.STRUCT:
        fields: list[exp.Expr] = []
        for column in data_type.expressions:
            assert isinstance(column, exp.ColumnDef)
            kind = column.args["kind"]
            assert isinstance(kind, exp.DataType)
            member = exp.Anonymous(
                this="struct_extract", expressions=[value.copy(), exp.Literal.string(column.name)]
            )
            fields.append(
                exp.PropertyEQ(
                    this=exp.to_identifier(column.name),
                    expression=finalize_value(member, kind, field, names),
                )
            )
        packed = exp.Anonymous(this="struct_pack", expressions=fields)
        return check(exp.Is(this=value.copy(), expression=exp.Null()), exp.Null(), packed)

    [element_type] = data_type.expressions
    assert isinstance(element_type, exp.DataType)
    var = f"crucible_e{next(names)}"
    element = exp.column(var)

    def each(body: exp.Expr) -> exp.Expr:
        lam = exp.Lambda(this=body, expressions=[exp.to_identifier(var)])
        return exp.Anonymous(this="list_transform", expressions=[value.copy(), lam])

    has_null = exp.Anonymous(
        this="list_bool_or", expressions=[each(exp.Is(this=element.copy(), expression=exp.Null()))]
    )
    body = (
        each(finalize_value(element, element_type, field, names))
        if _has_array(element_type)
        else value.copy()
    )
    message = f"Array cannot have a null element; error in writing field {field}"
    return exp.Case(
        ifs=[
            exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=exp.Array()),
            exp.If(this=has_null, true=raise_runtime(message)),
        ],
        default=body,
    )
