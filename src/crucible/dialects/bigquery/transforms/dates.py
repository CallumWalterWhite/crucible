"""Dates and times: TIMESTAMP_DIFF, EXTRACT(DAYOFWEEK|WEEK), FORMAT_DATE/DATETIME/TIMESTAMP `%e`.

Slice 2. Each rewrite is checked against recorded BigQuery answers (corpus) and DuckDB behaviour
(verified on DuckDB 1.5.6):

- TIMESTAMP_DIFF(a, b, part) counts whole parts, truncated toward zero. DuckDB date_sub(part, start,
  end) does the same, so it is date_sub(part, b, a).
- EXTRACT(DAYOFWEEK): BigQuery Sunday = 1, DuckDB dayofweek Sunday = 0.
- FORMAT `%e`: BigQuery pads the day to two characters with a space. SQLGlot converts `%e` to
  DuckDB's `%-d` (no padding), so the padded day is lpad(<%-d>, 2, ' ').
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import text, typed
from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind

if TYPE_CHECKING:
    from crucible.dialects.bigquery.transforms import TransformContext

# BigQuery's TIMESTAMP_DIFF date parts (the others are INVALID_QUERY below).
TIMESTAMP_DIFF_PARTS = frozenset({"MICROSECOND", "MILLISECOND", "SECOND", "MINUTE", "HOUR", "DAY"})
# SQLGlot's DuckDB form of BigQuery's `%e` (day, space-padded in BigQuery, not in DuckDB).
DAY_UNPADDED = "%-d"


@dataclass(frozen=True)
class _Token:
    kind: str  # "pct" (%%), "day" (%-d), "spec" (any other %x), "lit" (one character)
    text: str


def transform(tree: exp.Expr, ctx: TransformContext) -> exp.Expr:
    # Innermost first, so a call inside another rewrite's operand is rewritten too.
    for node in reversed(list(tree.find_all(exp.TimestampDiff, exp.Extract, exp.TimeToStr))):
        if isinstance(node, exp.TimestampDiff):
            node.replace(_timestamp_diff(node))
        elif isinstance(node, exp.Extract):
            replacement = _extract(node)
            if replacement is not None:
                node.replace(replacement)
        else:
            replacement = _format_day(node)
            if replacement is not None:
                node.replace(replacement)
    return tree


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
    if isinstance(part, exp.WeekStart) or (
        isinstance(part, exp.Var) and part.name.upper() == "WEEK"
    ):
        # BigQuery weeks start on Sunday (or the named day), DuckDB's are ISO: not translated.
        raise CrucibleUnsupportedError("EXTRACT(WEEK)")
    if not (isinstance(part, exp.Var) and part.name.upper() == "DAYOFWEEK"):
        return None
    value = node.expression
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError("EXTRACT(DAYOFWEEK) without a value")
    day = typed(exp.Anonymous(this="dayofweek", expressions=[value.copy()]), "INT64")
    return typed(exp.Add(this=day, expression=exp.Literal.number(1)), "INT64")


# --- FORMAT_DATE / FORMAT_DATETIME / FORMAT_TIMESTAMP: `%e` ------------------------------------
def _format_day(node: exp.TimeToStr) -> exp.Expr | None:
    fmt = node.args.get("format")
    if not isinstance(fmt, exp.Literal) or not fmt.is_string:
        return None  # a non-literal format is left as it is
    tokens = _tokens(str(fmt.this))
    if tokens is None:
        return None
    if _escaped_e(tokens):
        raise CrucibleUnsupportedError("FORMAT_DATE, FORMAT_DATETIME or FORMAT_TIMESTAMP with %%e")
    if not any(t.kind == "day" for t in tokens):
        return None
    value = node.this
    if not isinstance(value, exp.Expr):
        raise CrucibleUnsupportedError("FORMAT_* without a value")

    def strftime(pattern: str) -> exp.Expr:
        call = exp.TimeToStr(this=value.copy(), format=exp.Literal.string(pattern))
        return typed(call, "STRING")

    pieces: list[exp.Expr] = []
    pending = ""
    for token in tokens:
        if token.kind != "day":
            pending += token.text
            continue
        if pending:
            pieces.append(strftime(pending))
            pending = ""
        day = strftime(DAY_UNPADDED)
        pieces.append(
            typed(
                exp.Anonymous(
                    this="lpad", expressions=[day, exp.Literal.number(2), exp.Literal.string(" ")]
                ),
                "STRING",
            )
        )
    if pending:
        pieces.append(strftime(pending))
    return typed(text(*pieces), "STRING")


def _tokens(fmt: str) -> list[_Token] | None:
    """Split SQLGlot's DuckDB-form format into `%%`, `%-d`, other `%x` and single characters.

    None when the format ends in a lone `%`: that is left to DuckDB, as before.
    """
    tokens: list[_Token] = []
    index = 0
    while index < len(fmt):
        if fmt.startswith("%%", index):
            token = _Token("pct", "%%")
        elif fmt.startswith(DAY_UNPADDED, index):
            token = _Token("day", DAY_UNPADDED)
        elif fmt[index] == "%":
            if index + 1 >= len(fmt):
                return None
            token = _Token("spec", fmt[index : index + 2])
        else:
            token = _Token("lit", fmt[index])
        tokens.append(token)
        index += len(token.text)
    return tokens


def _escaped_e(tokens: list[_Token]) -> bool:
    """True where SQLGlot turned BigQuery's `%%e` (a literal `%e`) into `%%-d`.

    The pair `%%` followed by the characters `-d` is that mis-conversion, and BigQuery's literal
    `%%-d` looks the same, so both are refused. The original text is not available here.
    """
    for index, token in enumerate(tokens):
        if token.kind != "pct":
            continue
        following = "".join(t.text for t in tokens[index + 1 : index + 3])
        if following == "-d":
            return True
    return False
