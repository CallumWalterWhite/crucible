"""String functions: FORMAT, UPPER/LOWER/INITCAP case mapping, FARM_FINGERPRINT (farmhash.py).

FORMAT follows the BigQuery reference (string_functions, "FORMAT"), checked on 2025-era docs:
- a NULL argument makes the result NULL ("the function generally produces a NULL value if a NULL
  argument is present"; %t and %T are the exceptions, and are not translated here);
- a NULL format string makes the result NULL and ignores the other arguments;
- a wrong number of arguments, or a specifier that does not fit its argument, is an error.

Case mapping: BigQuery uses full Unicode case mapping (UPPER('ß') is 'SS', recorded); DuckDB uses
the simple mapping ('ẞ'). A character whose Python mapping is longer than one character cannot be
mapped exactly, so such input raises CrucibleUnsupportedError instead of a best guess.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery import farmhash, types
from crucible.dialects.bigquery.transforms.common import check, raise_unsupported, text, typed
from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

# %%, %d/%i, %s, %f and %.<N>f. Anything else (flags, width, other letters) is unsupported.
_SPEC = re.compile(r"%(?:(%)|([dis])|(?:\.(\d+))?(f))")
_ANY_SPEC = re.compile(r"%[^a-zA-Z%]*[a-zA-Z%]?")
DEFAULT_FLOAT_PRECISION = 6
# printf of a DOUBLE is exact to about 15 significant digits; NUMERIC %f relies on that.
NUMERIC_SIGNIFICANT_DIGITS = 15

_CASE_MAPPINGS = {"UPPER": str.upper, "LOWER": str.lower, "INITCAP": str.title}
# Specifier letter -> kind: %i is %d, and %f (no letter in the group) is %f.
_KINDS = {"d": "d", "i": "d", "s": "s", "f": "f"}


@dataclass(frozen=True)
class _Text:
    text: str


@dataclass(frozen=True)
class _Spec:
    kind: str  # "d" (INT64), "s" (STRING), "f" (FLOAT64 or NUMERIC)
    precision: int
    written: str  # the specifier as written, for messages


def _farm_fingerprint(node: exp.FarmFingerprint) -> exp.Expr:
    """Crucible's Python FarmHash (farmhash.py) over a STRING's UTF-8 bytes, or over BYTES."""
    [arg] = node.expressions
    kind = types.type_name(arg.type) if arg.type is not None else None
    if kind == "STRING":
        arg = exp.Anonymous(this="encode", expressions=[arg])  # UTF-8 bytes
    elif kind != "BYTES":
        raise CrucibleUnsupportedError(f"FARM_FINGERPRINT of a {kind or 'value of unknown type'}")
    call = exp.Anonymous(this=farmhash.FUNCTION, expressions=[arg])
    call.type = exp.DataType.build("INT64", dialect="bigquery")
    return call


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    # Innermost first, so a call inside another rewrite's operand is rewritten too.
    for node in reversed(
        list(tree.find_all(exp.Format, exp.Upper, exp.Lower, exp.Initcap, exp.FarmFingerprint))
    ):
        if isinstance(node, exp.Format):
            node.replace(_format(node))
        elif isinstance(node, exp.FarmFingerprint):
            node.replace(_farm_fingerprint(node))
        elif isinstance(node, exp.Upper):
            node.replace(_case_mapping(node, "UPPER"))
        elif isinstance(node, exp.Lower):
            node.replace(_case_mapping(node, "LOWER"))
        else:
            node.replace(_case_mapping(node, "INITCAP"))
    return tree


# --- FORMAT ------------------------------------------------------------------------------------
def _format(node: exp.Format) -> exp.Expr:
    template = node.this
    args = list(node.expressions)
    if isinstance(template, exp.Null):
        # A NULL format string gives NULL, and the other arguments are ignored.
        return typed(exp.Null(), "STRING")
    literal = _string_literal(template)
    if literal is None:
        raise CrucibleUnsupportedError("FORMAT with a format string that is not a literal")
    items = _parse(literal)
    specs = [item for item in items if isinstance(item, _Spec)]
    if len(specs) != len(args):
        raise CrucibleError(
            ErrorKind.INVALID_QUERY,
            f"FORMAT has {len(specs)} format specifiers but {len(args)} arguments",
        )
    if any(isinstance(arg, exp.Null) for arg in args):
        # A NULL argument (literal) gives NULL, as the documentation states.
        return typed(exp.Null(), "STRING")
    pieces: list[exp.Expr] = []
    values = iter(args)
    for item in items:
        if isinstance(item, _Text):
            pieces.append(exp.Literal.string(item.text))
        else:
            pieces.append(_render(item, next(values)))
    if not pieces:
        return typed(exp.Literal.string(""), "STRING")
    return typed(text(*pieces), "STRING")


def _string_literal(node: exp.Expr) -> str | None:
    if isinstance(node, exp.RawString):
        return str(node.this)
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this)
    return None


def _parse(template: str) -> list[_Text | _Spec]:
    items: list[_Text | _Spec] = []
    pending = ""
    index = 0
    while index < len(template):
        if template[index] != "%":
            pending += template[index]
            index += 1
            continue
        match = _SPEC.match(template, index)
        if match is None:
            bad = _ANY_SPEC.match(template, index)
            written = bad.group(0) if bad else "%"
            raise CrucibleUnsupportedError(f"FORMAT specifier {written!r}")
        if match.group(1):  # %%
            pending += "%"
        else:
            if pending:
                items.append(_Text(pending))
                pending = ""
            kind = _KINDS.get(match.group(2) or "f", "f")
            precision = int(match.group(3)) if match.group(3) else DEFAULT_FLOAT_PRECISION
            items.append(_Spec(kind, precision, match.group(0)))
        index = match.end()
    if pending:
        items.append(_Text(pending))
    return items


def _render(spec: _Spec, arg: exp.Expr) -> exp.Expr:
    arg_type = _argument_type(arg)
    if spec.kind == "d":
        _require(spec, arg_type, "INT64")
        return _printf("%d", arg)
    if spec.kind == "s":
        _require(spec, arg_type, "STRING")
        return _printf("%s", arg)
    if arg_type == "FLOAT64":
        return _printf(f"%.{spec.precision}f", arg)
    if arg_type == "NUMERIC":
        return _numeric_float(spec, arg)
    raise CrucibleUnsupportedError(f"FORMAT {spec.written} with a {arg_type} argument")


def _require(spec: _Spec, arg_type: str, wanted: str) -> None:
    if arg_type != wanted:
        raise CrucibleUnsupportedError(f"FORMAT {spec.written} with a {arg_type} argument")


def _argument_type(arg: exp.Expr) -> str:
    """BigQuery's name for an argument's type. A literal is typed by BigQuery's literal rules
    (literals.py): an integer is INT64, a literal with a fraction or exponent is FLOAT64."""
    if isinstance(arg, exp.Literal) and not arg.is_string:
        digits = str(arg.this)
        if digits.isdigit() or digits.lower().startswith("0x"):
            return "INT64"
        return "FLOAT64"
    if arg.type is None:
        raise CrucibleUnsupportedError("FORMAT with an argument of unknown type")
    return types.type_name(arg.type)


def _printf(pattern: str, arg: exp.Expr) -> exp.Expr:
    # printf returns NULL for a NULL argument, and || then makes the whole result NULL.
    call = exp.Anonymous(this="printf", expressions=[exp.Literal.string(pattern), arg.copy()])
    return typed(call, "STRING")


def _numeric_float(spec: _Spec, arg: exp.Expr) -> exp.Expr:
    """%f of a NUMERIC argument. BigQuery's rounding of NUMERIC is not documented here, so the
    value is printed only when it already has at most `precision` decimals and few enough digits
    for DOUBLE. Otherwise the query raises CrucibleUnsupportedError at run time."""
    if spec.precision > NUMERIC_SIGNIFICANT_DIGITS:
        raise CrucibleUnsupportedError(f"FORMAT {spec.written} of a NUMERIC argument")
    limit = 10 ** (NUMERIC_SIGNIFICANT_DIGITS - spec.precision)
    rounded = exp.Anonymous(
        this="round", expressions=[arg.copy(), exp.Literal.number(spec.precision)]
    )
    needs_rounding = exp.NEQ(this=rounded, expression=arg.copy())
    too_large = exp.GTE(
        this=exp.Anonymous(this="abs", expressions=[arg.copy()]),
        expression=exp.Literal.number(limit),
    )
    failure = raise_unsupported(
        f"FORMAT {spec.written} of a NUMERIC value that needs rounding or has too many digits"
    )
    as_double = exp.cast(arg.copy(), "DOUBLE")
    printed = _printf(f"%.{spec.precision}f", as_double)
    return typed(
        check(exp.Or(this=needs_rounding, expression=too_large), failure, printed), "STRING"
    )


# --- UPPER / LOWER / INITCAP -------------------------------------------------------------------
@functools.cache
def _multi_character_class(name: str) -> str:
    """A regular-expression class of every character whose case mapping is longer than one."""
    mapping = _CASE_MAPPINGS[name]
    chars = [
        chr(code)
        for code in range(0x110000)
        if not 0xD800 <= code <= 0xDFFF and len(mapping(chr(code))) > 1
    ]
    return "[" + "".join(f"\\x{{{ord(char):x}}}" for char in chars) + "]"


# LOWER: the only character with a multi-character lowercase is U+0130 (recorded: LOWER('İ') is
# 'i' + U+0307), so it is replaced first; capital sigma lowercases by context (final ς), which
# DuckDB cannot see, so it still raises.
_LOWER_SPECIAL = "\u0130"
_LOWER_CONTEXTUAL = "[\u03a3]"


def _lower(node: exp.Expr) -> exp.Expr:
    value = node.this
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError("LOWER without a value")
    replaced = exp.Anonymous(
        this="replace",
        expressions=[
            value.copy(),
            exp.Literal.string(_LOWER_SPECIAL),
            exp.Literal.string("i\u0307"),
        ],
    )
    lowered = node.copy()
    lowered.set("this", replaced)
    contextual = exp.Anonymous(
        this="regexp_matches", expressions=[value.copy(), exp.Literal.string(_LOWER_CONTEXTUAL)]
    )
    failure = raise_unsupported("LOWER of a capital sigma (its lowercase depends on context)")
    return typed(check(contextual, failure, lowered), "STRING")


def _case_mapping(node: exp.Expr, name: str) -> exp.Expr:
    if name == "LOWER":
        return _lower(node)
    value = node.this
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError(f"{name} without a value")
    has_special = exp.Anonymous(
        this="regexp_matches",
        expressions=[value.copy(), exp.Literal.string(_multi_character_class(name))],
    )
    failure = raise_unsupported(f"{name} of a character with a multi-character case mapping")
    return typed(check(has_special, failure, node.copy()), "STRING")
