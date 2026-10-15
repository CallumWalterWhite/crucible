"""Dates and times: TIMESTAMP_DIFF, EXTRACT (DAYOFWEEK, ISOWEEK, ISOYEAR, WEEK), time zones.

Each rewrite is checked against DuckDB 1.5.6 behaviour and, where a recorded case exists, the
corpus:

- TIMESTAMP_DIFF(a, b, part) counts whole parts, truncated toward zero; DuckDB date_sub(part, start,
  end) does the same, so it is date_sub(part, b, a).
- EXTRACT(DAYOFWEEK): BigQuery Sunday = 1, DuckDB dayofweek Sunday = 0.
- EXTRACT(ISOWEEK) and EXTRACT(ISOYEAR) are DuckDB's weekofyear and isoyear (ISO 8601).
- A time zone argument must be applied, never dropped. SQLGlot keeps it on a `zone` argument of
  FORMAT_*, PARSE_TIMESTAMP and STRING, and the DuckDB generator does not emit it.
  FORMAT_TIMESTAMP and PARSE_TIMESTAMP are translated with an explicit AT TIME ZONE; the rest are
  refused or guarded.
- FORMAT `%-` elements: SQLGlot converts BigQuery's `%e` to DuckDB's `%-d`, and the original text is
  not kept on the parsed node, so `%-d` is refused whatever its origin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery import types
from crucible.dialects.bigquery.transforms.common import (
    check,
    raise_unsupported,
    rewrite_bottom_up,
    typed,
)
from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

# BigQuery's TIMESTAMP_DIFF date parts (the others are INVALID_QUERY below).
TIMESTAMP_DIFF_PARTS = frozenset({"MICROSECOND", "MILLISECOND", "SECOND", "MINUTE", "HOUR", "DAY"})
# Format elements that print a zone name or offset. A DuckDB naive timestamp has neither.
ZONE_ELEMENTS = frozenset({"%Z", "%z", "%E"})
# A string BigQuery reads as a plain date, optionally with a time (no offset, no zone name).
PLAIN_DATETIME = r"^\s*\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{1,2}(:\d{1,2}(\.\d+)?)?)?\s*$"
# BigQuery part -> DuckDB function, for parts with the same meaning (ISO 8601 weeks and years).
_ISO_FUNCTIONS = {"ISOWEEK": "weekofyear", "ISOYEAR": "isoyear"}


@dataclass(frozen=True)
class _Token:
    kind: str  # "pct" (%%), "day" (%-d), "spec" (any other %x), "lit" (one character)
    text: str


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    return rewrite_bottom_up(tree, _rewrite)


def _rewrite(node: exp.Expr) -> exp.Expr:
    if isinstance(node, exp.TimestampDiff):
        replacement = _timestamp_diff(node)
    elif isinstance(node, exp.Extract):
        replacement = _extract(node)
    elif isinstance(node, exp.TimeToStr):
        replacement = _format(node)
    elif isinstance(node, exp.StrToTime):
        replacement = _parse(node)
    elif isinstance(node, exp.Timestamp):
        replacement = _timestamp(node)
    elif isinstance(node, exp.String):
        replacement = _string(node)
    elif isinstance(node, exp.CurrentDatetime):
        replacement = _current_datetime(node)
    else:
        return node
    return node if replacement is None else replacement


# --- TIMESTAMP_DIFF --------------------------------------------------------------------------
def _timestamp_diff(node: exp.TimestampDiff) -> exp.Expr:
    unit = node.args.get("unit")
    if not isinstance(unit, exp.Var):
        raise CrucibleUnsupportedError("TIMESTAMP_DIFF with a date part that is not a keyword")
    part = unit.name.upper()
    if part not in TIMESTAMP_DIFF_PARTS:
        raise CrucibleError(
            ErrorKind.INVALID_QUERY, f"TIMESTAMP_DIFF does not support the {part} date part"
        )
    later, earlier = node.this, node.expression
    if not isinstance(later, exp.Expr) or not isinstance(earlier, exp.Expr):
        raise CrucibleUnsupportedError("TIMESTAMP_DIFF without two timestamps")
    # date_sub(part, start, end) counts whole parts from start to end; BigQuery counts a - b.
    call = exp.Anonymous(
        this="date_sub",
        expressions=[exp.Literal.string(part.lower()), earlier.copy(), later.copy()],
    )
    return typed(call, "INT64")


# --- EXTRACT -----------------------------------------------------------------------------------
def _extract(node: exp.Extract) -> exp.Expr | None:
    part = node.this
    if isinstance(part, exp.WeekStart):
        # WEEK(<weekday>): weeks starting on another day; unrecorded, not translated.
        raise CrucibleUnsupportedError("EXTRACT(WEEK(<weekday>))")
    if isinstance(part, exp.Var) and part.name.upper() == "WEEK":
        # BigQuery WEEK: weeks start on Sunday, days before the year's first Sunday are week 0
        # (recorded: 2024-01-07, a Sunday, is week 1). DuckDB's week is ISO.
        value = node.expression
        day_of_year = exp.Anonymous(this="dayofyear", expressions=[value.copy()])
        weekday = exp.Anonymous(this="dayofweek", expressions=[value.copy()])  # Sunday = 0
        shifted = exp.Sub(
            this=exp.Add(this=day_of_year, expression=exp.Literal.number(6)), expression=weekday
        )
        return typed(
            exp.IntDiv(this=exp.Paren(this=shifted), expression=exp.Literal.number(7)), "INT64"
        )
    if not isinstance(part, exp.Var):
        return None
    name = part.name.upper()
    if name not in ("DAYOFWEEK", *_ISO_FUNCTIONS):
        return None
    value = node.expression
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError(f"EXTRACT({name}) without a value")
    if name in _ISO_FUNCTIONS:
        return typed(exp.Anonymous(this=_ISO_FUNCTIONS[name], expressions=[value.copy()]), "INT64")
    day = typed(exp.Anonymous(this="dayofweek", expressions=[value.copy()]), "INT64")
    return typed(exp.Add(this=day, expression=exp.Literal.number(1)), "INT64")


# --- FORMAT_DATE / FORMAT_DATETIME / FORMAT_TIMESTAMP ------------------------------------------
def _format(node: exp.TimeToStr) -> exp.Expr | None:
    value = node.this
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError("FORMAT_* without a value")
    zone = node.args.get("zone")
    fmt = node.args.get("format")
    if zone is None:
        if isinstance(fmt, exp.Literal) and fmt.is_string:
            _refuse_percent_dash(str(fmt.this))
        return None
    if isinstance(value, exp.TsOrDsToDate | exp.TsOrDsToDatetime):
        # BigQuery's FORMAT_DATE and FORMAT_DATETIME take two arguments: the zone is rejected.
        name = "FORMAT_DATE" if isinstance(value, exp.TsOrDsToDate) else "FORMAT_DATETIME"
        raise CrucibleError(ErrorKind.INVALID_QUERY, f"{name} takes two arguments, not three")
    name = "FORMAT_TIMESTAMP"
    if not (isinstance(fmt, exp.Literal) and fmt.is_string):
        raise CrucibleUnsupportedError(f"{name} with a time zone and a non-literal format")
    tokens = _tokens(str(fmt.this))
    if tokens is None:
        raise CrucibleUnsupportedError(f"{name} with a time zone and a trailing %")
    _refuse_zone_elements(tokens, name)
    _refuse_percent_dash(str(fmt.this))
    source = value.this if isinstance(value, exp.TsOrDsToTimestamp) else value
    # The wall-clock time in the zone, which strftime then prints.
    local = exp.AtTimeZone(this=exp.cast(source.copy(), "TIMESTAMPTZ"), zone=zone.copy())
    return typed(exp.TimeToStr(this=local, format=fmt.copy()), "STRING")


def _refuse_percent_dash(fmt: str) -> None:
    """Refuse `%-d`, and any other `%-` element, in a FORMAT_* format.

    `%e` arrives as `%-d` (see the module docstring), so a literal `%e` is refused too. A `%%-d`
    is refused because SQLGlot turns BigQuery's `%%e` into it, and the two cannot be told apart.
    """
    tokens = _tokens(fmt)
    if tokens is None:
        return
    element = _dash_element(tokens)
    if element is not None:
        raise CrucibleUnsupportedError(f"format element %-{element}")


def _dash_element(tokens: list[_Token]) -> str | None:
    for index, token in enumerate(tokens):
        if token.kind == "day":
            return "d"
        if token.kind == "spec" and token.text == "%-":
            return tokens[index + 1].text if index + 1 < len(tokens) else ""
        if token.kind == "pct" and "".join(t.text for t in tokens[index + 1 : index + 3]) == "-d":
            return "d"
    return None


def _refuse_zone_elements(tokens: list[_Token], name: str) -> None:
    for token in tokens:
        if token.kind == "spec" and token.text in ZONE_ELEMENTS:
            raise CrucibleUnsupportedError(f"{name} element {token.text} with a time zone argument")


def _tokens(fmt: str) -> list[_Token] | None:
    """Split a format into `%%`, `%-d`, other `%x` and single characters.

    None when the format ends in a lone `%`.
    """
    tokens: list[_Token] = []
    index = 0
    while index < len(fmt):
        if fmt.startswith("%%", index):
            token = _Token("pct", "%%")
        elif fmt.startswith("%-d", index):
            token = _Token("day", "%-d")
        elif fmt[index] == "%":
            if index + 1 >= len(fmt):
                return None
            token = _Token("spec", fmt[index : index + 2])
        else:
            token = _Token("lit", fmt[index])
        tokens.append(token)
        index += len(token.text)
    return tokens


# --- PARSE_TIMESTAMP ---------------------------------------------------------------------------
def _parse(node: exp.StrToTime) -> exp.Expr | None:
    zone = node.args.get("zone")
    if zone is None:
        return None
    fmt = node.args.get("format")
    if not (isinstance(fmt, exp.Literal) and fmt.is_string):
        raise CrucibleUnsupportedError("PARSE_TIMESTAMP with a time zone and a non-literal format")
    tokens = _tokens(str(fmt.this))
    if tokens is None:
        raise CrucibleUnsupportedError("PARSE_TIMESTAMP with a time zone and a trailing %")
    _refuse_zone_elements(tokens, "PARSE_TIMESTAMP")
    # STRPTIME reads the wall clock as an instant in UTC (the engine's base zone). Casting back to
    # TIMESTAMP recovers that wall clock, and AT TIME ZONE reads it in the argument's zone.
    wall = node.copy()
    wall.set("zone", None)
    local = exp.AtTimeZone(this=exp.cast(wall, "TIMESTAMP"), zone=zone.copy())
    return typed(exp.cast(local, "TIMESTAMPTZ"), "TIMESTAMP")


# --- TIMESTAMP(value, time_zone) and STRING(timestamp, time_zone) ------------------------------
def _timestamp(node: exp.Timestamp) -> exp.Expr | None:
    zone = node.args.get("zone")
    if zone is None:
        return None
    arg = node.this
    if not isinstance(arg, exp.Expr):
        raise CrucibleUnsupportedError("TIMESTAMP with a time zone and no value")
    translated = exp.cast(
        exp.AtTimeZone(this=exp.cast(arg.copy(), "TIMESTAMP"), zone=zone.copy()), "TIMESTAMPTZ"
    )
    if _may_be_string(arg):
        # A string may carry its own offset or zone name, and BigQuery's rule for that is not
        # verified here. Plain date and time strings are translated; any other string raises.
        plain = exp.Anonymous(
            this="regexp_matches",
            expressions=[exp.cast(arg.copy(), "VARCHAR"), exp.Literal.string(PLAIN_DATETIME)],
        )
        failure = raise_unsupported(
            "TIMESTAMP(string, time_zone) with a string that has an offset or zone name"
        )
        translated = check(exp.Not(this=plain), failure, translated)
    return typed(translated, "TIMESTAMP")


def _may_be_string(arg: exp.Expr) -> bool:
    if arg.type is None:
        return True
    try:
        return types.type_name(arg.type) == "STRING"
    except CrucibleUnsupportedError:
        return True


def _string(node: exp.String) -> exp.Expr | None:
    if node.args.get("zone") is not None:
        # The output format of STRING(timestamp, time_zone) is not verified, so it is refused.
        raise CrucibleUnsupportedError("STRING(timestamp, time_zone)")
    return None


# --- CURRENT_DATETIME ----------------------------------------------------------------------------
def _current_datetime(node: exp.CurrentDatetime) -> exp.Expr:
    # BigQuery's default zone is UTC; the explicit argument is the zone.
    zone = node.this if isinstance(node.this, exp.Expr) else exp.Literal.string("UTC")
    local = exp.AtTimeZone(this=exp.CurrentTimestamp(), zone=zone.copy())
    return typed(exp.cast(local, "TIMESTAMP"), "DATETIME")
