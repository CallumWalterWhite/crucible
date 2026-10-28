"""BigQuery's NaN rules for FLOAT64, where DuckDB's differ.

DuckDB treats NaN as equal to itself and greater than every number. BigQuery follows IEEE-754 for
comparisons and orders NaN after NULL and before -inf
(https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types#floating_point_semantics;
recorded: math/nan_comparisons_are_false, ordering/nan_ordering_asc, aggregate/min_max_with_nan):

- comparisons (=, <, <=, >, >=) with a NaN operand are FALSE, != is TRUE; BETWEEN and IN lists
  are rewritten into those comparisons;
- ORDER BY (queries, windows, aggregates) sorts NaN next to -inf, on the NULL side;
- MIN, LEAST and GREATEST return NaN when an input is NaN (MAX already does in DuckDB);
- NULLIF(a, b) returns a when b is NaN, since NaN equals nothing;
- GROUP BY and DISTINCT already treat every NaN as one value, as BigQuery does.

Other constructs that compare FLOAT64 values (IN subqueries, IS DISTINCT FROM, simple CASE,
range-offset window frames) are refused rather than guessed.
"""

from __future__ import annotations

from sqlglot import exp

from crucible.dialects.bigquery.transforms.common import rewrite_bottom_up, typed
from crucible.engine import CrucibleUnsupportedError

T = exp.DType
_FLOATS = {T.DOUBLE, T.FLOAT}
_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


def transform(tree: exp.Expr, ctx: object) -> exp.Expr:
    return rewrite_bottom_up(tree, _rewrite)


def _is_float(node: exp.Expr | None) -> bool:
    return isinstance(node, exp.Expr) and node.type is not None and node.type.this in _FLOATS


def _isnan(value: exp.Expr) -> exp.Expr:
    return typed(exp.Anonymous(this="isnan", expressions=[value.copy()]), "BOOL")


def _nan() -> exp.Expr:
    return typed(exp.cast(exp.Literal.string("NaN"), exp.DType.DOUBLE), "FLOAT64")


def _any(conditions: list[exp.Expr]) -> exp.Expr:
    out = conditions[0]
    for condition in conditions[1:]:
        out = typed(exp.Or(this=out, expression=condition), "BOOL")
    return out


def _comparison(node: exp.Expr) -> exp.Expr:
    sides = [s for s in (node.this, node.expression) if _is_float(s)]
    if not sides:
        return node
    outcome = exp.true() if isinstance(node, exp.NEQ) else exp.false()
    case = exp.Case(ifs=[exp.If(this=_any([_isnan(s) for s in sides]), true=outcome)], default=node)
    return typed(case, "BOOL")


def _rewrite(node: exp.Expr) -> exp.Expr:
    if isinstance(node, _COMPARISONS):
        return _comparison(node)
    if isinstance(node, exp.Between) and any(
        _is_float(n) for n in (node.this, node.args.get("low"), node.args.get("high"))
    ):
        low = _comparison(
            typed(exp.GTE(this=node.this.copy(), expression=node.args["low"]), "BOOL")
        )
        high = _comparison(
            typed(exp.LTE(this=node.this.copy(), expression=node.args["high"]), "BOOL")
        )
        return typed(exp.And(this=low, expression=high), "BOOL")
    if isinstance(node, exp.In) and (
        _is_float(node.this) or any(_is_float(e) for e in node.expressions)
    ):
        if node.args.get("query") is not None or node.args.get("unnest") is not None:
            raise CrucibleUnsupportedError("IN over FLOAT64 values that may be NaN")
        equal = [
            _comparison(typed(exp.EQ(this=node.this.copy(), expression=e.copy()), "BOOL"))
            for e in node.expressions
        ]
        return typed(exp.Paren(this=_any(equal)), "BOOL")
    if isinstance(node, exp.NullSafeEQ | exp.NullSafeNEQ) and (
        _is_float(node.this) or _is_float(node.expression)
    ):
        raise CrucibleUnsupportedError("IS [NOT] DISTINCT FROM over FLOAT64 values that may be NaN")
    if isinstance(node, exp.Nullif) and _is_float(node.expression):
        # The inner NULLIF turns a NaN b into NULL (DuckDB's NaN equals NaN), so NULLIF(NaN, NaN)
        # is NaN, as under BigQuery's IEEE comparison; a NULL b already returns a.
        b = node.expression
        inner = exp.Nullif(this=b.copy(), expression=_nan())
        node.set("expression", typed(inner, b.type or "FLOAT64"))
        return node
    if isinstance(node, exp.Case) and _is_float(node.this):
        raise CrucibleUnsupportedError("CASE <FLOAT64 value> WHEN ... (NaN equality)")
    if isinstance(node, exp.Order):
        return _order(node)
    if isinstance(node, exp.Window):
        return _window(node)
    if (
        isinstance(node, exp.Min)
        and _is_float(node.this)
        and not isinstance(node.parent, exp.Window)
    ):
        return _nan_if_any(node, exp.LogicalOr(this=_isnan(node.this)))
    if isinstance(node, exp.Greatest | exp.Least):
        values = [node.this, *node.expressions]
        if any(_is_float(v) for v in values):
            nulls = _any(
                [typed(exp.Is(this=v.copy(), expression=exp.Null()), "BOOL") for v in values]
            )
            case = exp.Case(
                ifs=[
                    exp.If(this=nulls, true=typed(exp.Null(), "FLOAT64")),
                    exp.If(this=_any([_isnan(v) for v in values if _is_float(v)]), true=_nan()),
                ],
                default=node,
            )
            return typed(case, node.type or "FLOAT64")
    return node


def _nan_if_any(value: exp.Expr, any_nan: exp.Expr) -> exp.Expr:
    typed(any_nan, "BOOL")
    case = exp.Case(ifs=[exp.If(this=any_nan, true=_nan())], default=value)
    return typed(case, value.type or "FLOAT64")


def _window(node: exp.Window) -> exp.Expr:
    inner = node.this
    if isinstance(inner, exp.Min) and _is_float(inner.this):
        flag = node.copy()
        flag.set("this", exp.LogicalOr(this=_isnan(inner.this)))
        return _nan_if_any(node, typed(flag, "BOOL"))
    return node


def _order(node: exp.Order) -> exp.Expr:
    keys: list[exp.Expr] = []
    changed = False
    for ordered in node.expressions:
        if isinstance(ordered, exp.Ordered) and _is_float(ordered.this):
            window = node.parent if isinstance(node.parent, exp.Window) else None
            spec = window.args.get("spec") if window is not None else None
            if (
                isinstance(spec, exp.WindowSpec)
                and str(spec.args.get("kind") or "").upper() == "RANGE"
            ):
                bounds = [spec.args.get("start"), spec.args.get("end")]
                if any(
                    not isinstance(b, str) or b.upper() not in ("UNBOUNDED", "CURRENT ROW")
                    for b in bounds
                    if b
                ):
                    raise CrucibleUnsupportedError("a RANGE window frame over a FLOAT64 ORDER BY")
            value = ordered.this
            # NULL stays NULL, NaN sorts before every number: the key's NULL ordering and
            # direction follow the original key's.
            key = exp.Case(
                ifs=[
                    exp.If(
                        this=typed(exp.Is(this=value.copy(), expression=exp.Null()), "BOOL"),
                        true=exp.Null(),
                    ),
                    exp.If(this=_isnan(value), true=exp.Literal.number(0)),
                ],
                default=exp.Literal.number(1),
            )
            extra = ordered.copy()
            extra.set("this", typed(key, "INT64"))
            keys.append(extra)
            changed = True
        keys.append(ordered)
    if changed:
        node.set("expressions", keys)
    return node
