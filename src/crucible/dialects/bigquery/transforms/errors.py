"""Runtime error parity (ADR 0011): raise wherever BigQuery raises, with BigQuery's message.

- `x / y`, `DIV(x, y)`, `MOD(x, y)` with y = 0: "division by zero: x / y" (DuckDB: NULL or inf).
- `CAST(string AS INT64)`: BigQuery accepts an optional sign and decimal digits or a 0x hex number;
  DuckDB also accepts '1.5' (rounding to 2). Anything else is "Bad int64 value: <s>".
- `CAST(float AS INT64)` rounds half away from zero in BigQuery; DuckDB's cast rounds half to even,
  so the value goes through ROUND (which rounds half away from zero) first.
- `arr[OFFSET(i)]` / `arr[ORDINAL(i)]` out of range: "Array index i is out of bounds (overflow)"
  or "(underflow)" (DuckDB gives NULL).
- `IEEE_DIVIDE(x, y)`: IEEE semantics, never an error.

SAFE_CAST, SAFE_DIVIDE and SAFE_OFFSET/SAFE_ORDINAL return NULL instead (the `safe` transform
handles the SAFE. prefix by wrapping these checks in TRY). NULL elements and NULL arrays in results
are handled where results are written (translate.py, `finalize_value`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import (
    T,
    check,
    duck_cast,
    raise_runtime,
    raise_unsupported,
    rewrite_bottom_up,
    typed,
)
from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

# BigQuery STRING -> INT64 (Conversion rules, "Casting"): optional sign, then digits or 0x hex.
# Whitespace handling is not documented: such values raise unsupported until a recording settles it.
INT64_TEXT = r"^[+-]?([0-9]+|0[xX][0-9a-fA-F]+)$"
INT64_TEXT_WITH_SPACE = r"^\s*[+-]?([0-9]+|0[xX][0-9a-fA-F]+)\s*$"
# Canonical formats for STRING -> DATE / DATETIME / TIMESTAMP casts (Conversion functions, "CAST
# AS DATE" etc.): date YYYY-[M]M-[D]D; time [H]H:[M]M:[S]S[.F] with up to 6 fractional digits;
# TIMESTAMP may add a zone: Z, +HH[:MM], or a space and a zone name.
_DATE = r"[0-9]{1,4}-[0-9]{1,2}-[0-9]{1,2}"
_TIME = r"[0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}(\.[0-9]{1,6})?"
_ZONE = r"(Z|z|[+-][0-9]{1,2}(:[0-9]{2})?| ?[A-Za-z_]+(/[A-Za-z_]+)*)"
TEMPORAL_TEXT = {
    T.DATE: ("date", rf"^{_DATE}$"),
    T.TIMESTAMP: ("datetime", rf"^{_DATE}([ Tt]{_TIME})?$"),
    T.TIMESTAMPTZ: ("timestamp", rf"^{_DATE}([ Tt]{_TIME})?{_ZONE}?$"),
}
_VOLATILE = re.compile(r"\b(rand|generate_uuid)\s*\(", re.IGNORECASE)


def _is(node: exp.Expr, *kinds: exp.DType) -> bool:
    return node.type is not None and node.type.this in kinds


def _zero_check(node: exp.Binary, operator: str) -> exp.Expr:
    divisor = node.expression
    if _VOLATILE.search(divisor.sql("bigquery")):
        # The check evaluates the divisor twice; a volatile divisor could differ between the two.
        raise CrucibleUnsupportedError(f"a non-deterministic divisor in {node.sql('bigquery')}")
    failure = raise_runtime("division by zero: ", node.this.copy(), f" {operator} ", divisor.copy())
    zero = exp.EQ(this=divisor.copy(), expression=exp.Literal.number(0))
    return check(zero, failure, node)


def _cast_string_to_int64(node: exp.Cast) -> exp.Expr:
    """Parse sign and magnitude separately: DuckDB cannot cast a negative hex string, and BigQuery
    accepts -0x8000000000000000 (INT64's minimum), whose magnitude does not fit INT64."""
    value = node.this
    safe = isinstance(node, exp.TryCast)
    plain = exp.RegexpLike(this=value.copy(), expression=exp.Literal.string(INT64_TEXT))
    spaced = exp.RegexpLike(this=value.copy(), expression=exp.Literal.string(INT64_TEXT_WITH_SPACE))
    bad: exp.Expr = exp.Null() if safe else raise_runtime("Bad int64 value: ", value.copy())

    digits = exp.Anonymous(this="ltrim", expressions=[value.copy(), exp.Literal.string("+-")])
    is_hex = exp.RegexpLike(this=digits.copy(), expression=exp.Literal.string("^0[xX]"))
    magnitude = exp.Case(
        ifs=[exp.If(this=is_hex, true=duck_cast(duck_cast(digits.copy(), "UBIGINT"), "HUGEINT"))],
        default=duck_cast(digits, "HUGEINT"),
    )
    negative = exp.Anonymous(
        this="starts_with", expressions=[value.copy(), exp.Literal.string("-")]
    )
    signed = exp.Case(
        ifs=[exp.If(this=negative, true=exp.Neg(this=magnitude.copy()))], default=magnitude
    )
    cast = typed(node.__class__(this=signed.copy(), to=node.args["to"].copy()), "INT64")
    cast.meta["crucible_checked"] = True
    # Out of INT64 range is "Bad int64 value" too, not DuckDB's overflow text.
    in_range = exp.Between(
        this=signed, low=exp.Literal.number(-(2**63)), high=exp.Literal.number(2**63 - 1)
    )
    cast = check(exp.Not(this=exp.Paren(this=in_range)), bad.copy(), cast)
    return check(
        exp.Not(this=exp.Paren(this=plain)),
        check(
            spaced,
            raise_unsupported("CAST of a string with surrounding whitespace to INT64"),
            bad,
        ),
        cast,
    )


def _cast_string_to_temporal(node: exp.Cast, kind: exp.DType) -> exp.Expr:
    """BigQuery accepts only its canonical formats; DuckDB also accepts e.g. a time on a DATE."""
    name, pattern = TEMPORAL_TEXT[kind]
    value = node.this
    safe = isinstance(node, exp.TryCast)
    plain = exp.RegexpLike(this=value.copy(), expression=exp.Literal.string(pattern))
    trimmed = exp.Anonymous(this="trim", expressions=[value.copy()])
    spaced = exp.RegexpLike(this=trimmed, expression=exp.Literal.string(pattern))
    bad: exp.Expr = exp.Null() if safe else raise_runtime(f"Invalid {name}: '", value.copy(), "'")
    node.meta["crucible_checked"] = True
    return check(
        exp.Not(this=exp.Paren(this=plain)),
        check(
            spaced,
            raise_unsupported(f"CAST of a string with surrounding whitespace to {name.upper()}"),
            bad,
        ),
        node,
    )


def _cast_float_to_int64(node: exp.Cast) -> exp.Expr:
    rounded = typed(exp.Round(this=node.this.copy()), "FLOAT64")
    out = node.__class__(this=rounded, to=node.args["to"].copy())
    return typed(out, "INT64")


def _offset_check(node: exp.Bracket) -> exp.Expr:
    [index] = node.expressions
    base = int(node.args.get("offset") or 0)
    array = node.this
    length = exp.Anonymous(this="len", expressions=[array.copy()])
    message = ("Array index ", index.copy(), " is out of bounds")
    low = exp.LT(this=index.copy(), expression=exp.Literal.number(base))
    high = exp.GTE(
        this=index.copy(),
        expression=exp.Add(this=length, expression=exp.Literal.number(base)),
    )
    return check(
        low,
        raise_runtime(*message, " (underflow)"),
        check(high, raise_runtime(*message, " (overflow)"), node),
    )


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    def rewrite(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.DPipe) and any(
            isinstance(o, exp.Null) for o in (node.this, node.expression)
        ):
            # An analysis error in BigQuery (recorded message), not a NULL result.
            raise CrucibleError(ErrorKind.INVALID_QUERY, "Operands of || cannot be literal NULL")
        if node.meta.get("crucible_checked"):
            return node
        node.meta["crucible_checked"] = True
        if isinstance(node, exp.Div):
            return _zero_check(node, "/")
        if isinstance(node, exp.IntDiv):
            return _zero_check(node, "DIV")
        if isinstance(node, exp.Mod):
            return _zero_check(node, "MOD")
        if isinstance(node, exp.Anonymous) and node.name.upper() == "IEEE_DIVIDE":
            left, right = node.expressions
            div = exp.Div(
                this=duck_cast(left.copy(), "DOUBLE"), expression=duck_cast(right.copy(), "DOUBLE")
            )
            div.meta["crucible_checked"] = True  # IEEE division never raises
            return typed(div, "FLOAT64")
        if (
            isinstance(node, exp.Cast)
            and node.to.this in TEMPORAL_TEXT
            and _is(node.this, T.TEXT, T.VARCHAR)
        ):
            return _cast_string_to_temporal(node, node.to.this)
        if isinstance(node, exp.Cast) and _is(node.to, T.BIGINT, T.INT):
            if _is(node.this, T.TEXT, T.VARCHAR):
                return _cast_string_to_int64(node)
            if _is(node.this, T.DOUBLE, T.FLOAT):
                return _cast_float_to_int64(node)
        if (
            isinstance(node, exp.Bracket)
            and not node.args.get("safe")
            and node.args.get("offset") is not None
            and _is(node.this, T.ARRAY)
            and len(node.expressions) == 1
        ):
            return _offset_check(node)
        return node

    return rewrite_bottom_up(tree, rewrite)
