"""STRING and BYTES: BigQuery converts between them only by an explicit CAST, as UTF-8
(https://cloud.google.com/bigquery/docs/reference/standard-sql/conversion_functions#cast_as_bytes
and #cast_as_string). DuckDB's own casts use an escaped text form of a BLOB instead.

- CAST(STRING AS BYTES) is the string's UTF-8 bytes. DuckDB's cast reads `\\xNN` in the text as
  one byte and refuses other non-ASCII characters, so it becomes encode().
- CAST(BYTES AS STRING) decodes UTF-8. DuckDB's cast writes `\\xNN` for a non-ASCII byte, so it
  becomes decode(). Invalid UTF-8 is an error in BigQuery, whose message is not recorded, so it
  raises unsupported; SAFE_CAST gives NULL.
- Nothing converts implicitly. A STRING and a BYTES operand of one function or operator
  (CONCAT, ||, =, IN, COALESCE, IF, CASE, ...) is an error in BigQuery, where DuckDB casts one to
  the other; the message is not recorded, so it is refused.
- BYTES arguments of the functions SQLGlot runs on DuckDB's text form of a BLOB (LOWER, UPPER,
  REVERSE, REPLACE, STARTS_WITH, TRIM, LTRIM, RTRIM) are refused: that text has `\\xNN` escapes,
  so the answer can be wrong. STRING(BYTES) has no BigQuery signature and is refused too.
- A variable's DEFAULT or SET value of the other type (scripting.py marks those casts) is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import (
    VOLATILE,
    T,
    check,
    raise_unsupported,
    typed,
)
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

COERCION = "crucible_coercion"  # Cast.meta: a variable assignment, not a CAST the user wrote

_BYTES = (T.BLOB, T.BINARY, T.VARBINARY)
_STRING = (T.TEXT, T.VARCHAR)
_THROUGH_TEXT = (exp.Lower, exp.Upper, exp.Reverse, exp.Replace, exp.StartsWith, exp.Trim)
# Operands of these may differ in type: STRUCT fields, a CAST's target, FORMAT (strings.py
# refuses its BYTES arguments).
_MIXED_OK = (exp.Struct, exp.Cast, exp.Format)


def _is(node: object, kinds: tuple[exp.DType, ...]) -> bool:
    return isinstance(node, exp.Expr) and node.type is not None and node.type.this in kinds


def _operand_groups(node: exp.Expr) -> list[list[exp.Expr]]:
    """The operands that must have one type."""
    if isinstance(node, exp.Case):
        listed: list[exp.Expr] = node.args.get("ifs") or []
        ifs = [i for i in listed if isinstance(i, exp.If)]
        branches = [i.args.get("true") for i in ifs] + [node.args.get("default")]
        keys = [node.this, *(i.this for i in ifs)] if node.this else []
        return [[b for b in branches if b is not None], keys]
    if isinstance(node, exp.If):
        return [[v for v in (node.args.get("true"), node.args.get("false")) if v is not None]]
    return [list(node.iter_expressions())]


def _mixed(node: exp.Expr) -> bool:
    return any(
        any(_is(o, _STRING) for o in group) and any(_is(o, _BYTES) for o in group)
        for group in _operand_groups(node)
    )


def _name(node: exp.Expr) -> str:
    # A BYTES literal's SQL can hold raw non-UTF-8 bytes, so a function is named, not printed.
    return node.sql_name() if isinstance(node, exp.Func) else node.sql("bigquery")


def _cast(node: exp.Cast) -> exp.Expr:
    value = node.this
    to_bytes = _is(node.this, _STRING) and node.to.this in _BYTES
    to_string = _is(node.this, _BYTES) and node.to.this in _STRING
    if not (to_bytes or to_string) or node.meta.get("crucible_storage"):
        return node
    if node.meta.get(COERCION):
        raise CrucibleUnsupportedError(
            f"assigning a {'STRING' if to_bytes else 'BYTES'} value to a "
            f"{'BYTES' if to_bytes else 'STRING'} variable"
        )
    if node.args.get("format"):
        raise CrucibleUnsupportedError(f"CAST with FORMAT: {node.sql('bigquery')}")
    if to_bytes:
        return typed(exp.Anonymous(this="encode", expressions=[value]), "BYTES")
    decoded = typed(exp.Anonymous(this="decode", expressions=[value.copy()]), "STRING")
    tried = typed(exp.Anonymous(this="try", expressions=[decoded.copy()]), "STRING")
    if isinstance(node, exp.TryCast):
        return tried
    if VOLATILE.search(value.sql("bigquery")):
        # The check evaluates the value twice; a volatile value could differ between the two.
        raise CrucibleUnsupportedError("CAST of a non-deterministic BYTES value to STRING")
    invalid = exp.And(
        this=exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null())),
        expression=exp.Is(this=tried, expression=exp.Null()),
    )
    return check(invalid, raise_unsupported("CAST of BYTES that are not UTF-8 to STRING"), decoded)


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    # Innermost first, so a CAST inside another one's operand is rewritten too.
    for node in reversed(list(tree.walk())):
        if isinstance(node, exp.Cast):
            new = _cast(node)
            if new is not node:
                node.replace(new)
                if node is tree:
                    tree = new
            continue
        if isinstance(node, (*_THROUGH_TEXT, exp.String)) and any(
            _is(o, _BYTES) for o in node.iter_expressions()
        ):
            raise CrucibleUnsupportedError(f"{node.sql_name()} of BYTES")
        if (
            isinstance(node, (exp.Func, exp.Binary, exp.Predicate))
            and not isinstance(node, _MIXED_OK)
            and _mixed(node)
        ):
            raise CrucibleUnsupportedError(
                f"STRING and BYTES operands of {_name(node)} (BigQuery's error is not recorded)"
            )
    return tree
