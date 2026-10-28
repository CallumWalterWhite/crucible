"""STRING and BYTES: BigQuery converts between them only by an explicit CAST, as UTF-8
(https://cloud.google.com/bigquery/docs/reference/standard-sql/conversion_functions#cast_as_bytes
and #cast_as_string). DuckDB's own casts use an escaped text form of a BLOB instead.

- CAST(STRING AS BYTES) is the string's UTF-8 bytes. DuckDB's cast reads `\\xNN` in the text as
  one byte and refuses other non-ASCII characters, so it becomes encode().
- CAST(BYTES AS STRING) decodes UTF-8. DuckDB's cast writes `\\xNN` for a non-ASCII byte, so it
  becomes decode(). Invalid UTF-8 is BigQuery's runtime error (recorded:
  cast/cast_bytes_invalid_utf8_to_string_error); SAFE_CAST gives NULL.
- Nothing converts implicitly. A STRING and a BYTES operand of one function or operator
  (CONCAT, ||, =, IN, COALESCE, IF, CASE, ...) is an error in BigQuery, where DuckDB casts one to
  the other. CONCAT of STRING and BYTES arguments and `STRING = BYTES` give BigQuery's recorded
  errors (string/concat_string_and_bytes_error, string/compare_string_and_bytes_error); the
  others are not recorded, so they are refused.
- BYTES functions DuckDB has no BLOB version of run on the hex text, two digits per byte, so
  they stay exact: REVERSE reverses the digit pairs, STARTS_WITH and ENDS_WITH compare hex (both
  sides have an even length, so a match is byte-aligned), STRING_AGG with a delimiter joins hex
  and BIT_COUNT counts the bits of the BLOB cast to BIT.
- Other BYTES functions are refused: those SQLGlot runs on DuckDB's text form of a BLOB (LOWER,
  UPPER, REPLACE, TRIM, LTRIM, RTRIM), where `\\xNN` escapes could give wrong bytes; those it
  runs in ways that are wrong for BYTES (STRPOS and INSTR match hex digits across byte
  boundaries, LPAD and RPAD repeat the whole pattern and never truncate); and those DuckDB has
  no BLOB version of (LIKE, SUBSTR, SPLIT, the REGEXP functions, CHAR_LENGTH, and
  ARRAY_TO_STRING of BYTES). STRING(BYTES) has no BigQuery signature and is refused too.
- A variable's DEFAULT or SET value of the other type (scripting.py marks those casts) is an
  error. A STRING literal for a BYTES variable gives BigQuery's recorded error
  (scripting/declare_bytes_default_string_error); anything else is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.errors import invalid_at
from crucible.dialects.bigquery.transforms.common import (
    VOLATILE,
    T,
    check,
    duck_cast,
    raise_runtime,
    typed,
)
from crucible.engine import CrucibleUnsupportedError

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

COERCION = "crucible_coercion"  # Cast.meta: a variable assignment, not a CAST the user wrote

_BYTES = (T.BLOB, T.BINARY, T.VARBINARY)
_STRING = (T.TEXT, T.VARCHAR)
_REFUSED = (
    exp.Lower,
    exp.Upper,
    exp.Replace,
    exp.Trim,
    exp.String,
    exp.StrPosition,
    exp.Pad,
    exp.Like,
    exp.Substring,
    exp.Split,
    exp.RegexpLike,
    exp.RegexpExtract,
    exp.RegexpExtractAll,
    exp.RegexpReplace,
    exp.RegexpInstr,
)
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


def _call(name: str, kind: str, *args: exp.Expr) -> exp.Expr:
    return typed(exp.Anonymous(this=name, expressions=list(args)), kind)


def _hex(value: exp.Expr) -> exp.Expr:
    return _call("hex", "STRING", value)


def _exact(node: exp.Expr) -> exp.Expr | None:
    """The hex or BIT form of a BYTES function DuckDB has no BLOB version of; None if `node` is
    not one."""
    if isinstance(node, exp.Reverse) and _is(node.this, _BYTES):
        pairs = _call(
            "regexp_extract_all", "ARRAY<STRING>", _hex(node.this), exp.Literal.string("..")
        )
        reversed_ = _call("list_reverse", "ARRAY<STRING>", pairs)
        joined = _call("array_to_string", "STRING", reversed_, exp.Literal.string(""))
        return _call("unhex", "BYTES", joined)
    if isinstance(node, exp.StartsWith | exp.EndsWith) and _is(node.this, _BYTES):
        name = "starts_with" if isinstance(node, exp.StartsWith) else "ends_with"
        return _call(name, "BOOL", _hex(node.this), _hex(node.expression))
    if isinstance(node, exp.BitwiseCount) and _is(node.this, (T.BIGINT, T.INT)):
        # DuckDB keeps a small integer literal 32 bits wide; BigQuery counts INT64's 64 bits.
        node.set("this", duck_cast(node.this, "BIGINT"))
        return node
    if isinstance(node, exp.BitwiseCount) and _is(node.this, _BYTES):
        # DuckDB cannot cast an empty BLOB to BIT; CASE does not evaluate ELSE for it.
        empty = exp.EQ(
            this=_call("octet_length", "INT64", node.this.copy()), expression=exp.Literal.number(0)
        )
        bits = duck_cast(node.this, "BIT")
        counted = exp.Case(
            ifs=[exp.If(this=empty, true=exp.Literal.number(0))],
            default=_call("bit_count", "INT64", bits),
        )
        return typed(counted, "INT64")
    if isinstance(node, exp.GroupConcat) and _is(node, _BYTES) and node.args.get("separator"):
        holder: exp.Expr = node
        orders: list[exp.Order] = []
        while isinstance(holder.this, exp.Limit | exp.Order):
            holder = holder.this
            if isinstance(holder, exp.Order):
                orders.append(holder)
        if isinstance(holder.this, exp.Distinct):
            holder = holder.this
            values = list(holder.expressions)
            holder.set("expressions", [_hex(v) for v in values])
            # DuckDB orders a DISTINCT aggregate only by its argument; hex sorts as the bytes do.
            for ordered in (o for order in orders for o in order.expressions):
                if isinstance(ordered, exp.Ordered) and ordered.this in values:
                    ordered.set("this", _hex(ordered.this))
        else:
            holder.set("this", _hex(holder.this))
        node.set("separator", _hex(node.args["separator"]))
        typed(node, "STRING")
        window = node.parent
        if isinstance(window, exp.Window):  # unhex the window's value, not the aggregate in it
            typed(window, "STRING")
            window.replace(_call("unhex", "BYTES", window.copy()))
            return node
        return _call("unhex", "BYTES", node.copy())
    return None


def _refused(node: exp.Expr) -> bool:
    if isinstance(node, exp.Length) and not node.args.get("binary"):
        return _is(node.this, _BYTES)  # CHAR_LENGTH, which BigQuery defines for STRING only
    if isinstance(node, exp.ArrayToString):
        kind = node.this.type
        elements: list[exp.DataType] = kind.expressions if kind and kind.is_type(T.ARRAY) else []
        return bool(elements) and elements[0].this in _BYTES
    return isinstance(node, _REFUSED) and any(_is(o, _BYTES) for o in node.iter_expressions())


def _cast(node: exp.Cast) -> exp.Expr:
    value = node.this
    to_bytes = _is(node.this, _STRING) and node.to.this in _BYTES
    to_string = _is(node.this, _BYTES) and node.to.this in _STRING
    if not (to_bytes or to_string) or node.meta.get("crucible_storage"):
        return node
    if node.meta.get(COERCION):
        if to_bytes and isinstance(value, exp.Literal) and value.is_string:
            raise invalid_at(
                value,
                f"Query error: Cannot coerce expression {value.sql('bigquery')} to type BYTES",
            )
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
    return check(invalid, raise_runtime("Invalid cast of bytes to UTF8 string"), decoded)


def _mixed_error(node: exp.Expr) -> Exception:
    """BigQuery's recorded error for the mixed operands, where it is recorded; else a refusal."""
    if isinstance(node, exp.Concat):
        args = list(node.expressions)
        if all(_is(a, _STRING) or _is(a, _BYTES) for a in args):
            kinds = ["STRING" if _is(a, _STRING) else "BYTES" for a in args]
            lines = [
                f"No matching signature for function CONCAT\n  Argument types: {', '.join(kinds)}"
            ]
            for wanted, other in (("STRING", "BYTES"), ("BYTES", "STRING")):
                at = kinds.index(other) + 1
                lines.append(
                    f"  Signature: CONCAT({wanted}, [{wanted}, ...])\n    Argument {at}: Unable to "
                    f"coerce type {other} to expected type {wanted}"
                )
            return invalid_at(node, "\n".join(lines))
    if isinstance(node, exp.EQ) and _is(node.this, _STRING) and _is(node.expression, _BYTES):
        return invalid_at(
            node.this,
            "No matching signature for operator = for argument types: STRING, BYTES. STRING and "
            "BYTES are different types that are not directly comparable. To write a BYTES literal, "
            "use a b-prefixed literal such as b'bytes value'\n  Signature: T1 = T1\n    Unable to "
            "find common supertype for templated argument <T1>\n      Input types for <T1>: "
            "{STRING, BYTES}",
        )
    return CrucibleUnsupportedError(
        f"STRING and BYTES operands of {_name(node)} (BigQuery's error is not recorded)"
    )


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
        exact = _exact(node)
        if exact is not None:
            node.replace(exact)
            if node is tree:
                tree = exact
            continue
        if _refused(node):
            name = "LIKE" if isinstance(node, exp.Like) else node.meta.get("bigquery_name")
            raise CrucibleUnsupportedError(f"{name or node.key.upper()} of BYTES")
        if (
            isinstance(node, (exp.Func, exp.Binary, exp.Predicate))
            and not isinstance(node, _MIXED_OK)
            and _mixed(node)
        ):
            raise _mixed_error(node)
    return tree
