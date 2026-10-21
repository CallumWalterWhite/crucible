"""DuckDB generation for ASTs parsed from BigQuery SQL (ADR 0008).

Fixes for how a BigQuery construct must be written in DuckDB override generator methods here. This
class is never used for another source dialect.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.duckdb import DuckDB
from sqlglot.errors import ErrorLevel, UnsupportedError
from sqlglot.generators.duckdb import (  # pinned SQLGlot (ADR 0008): private helpers reused as is
    _escape_regex_metachars,  # pyright: ignore[reportPrivateUsage]
    _implicit_datetime_cast,  # pyright: ignore[reportPrivateUsage]
)

from crucible.engine import CrucibleUnsupportedError


class BigQueryToDuckDB(DuckDB):
    class Generator(DuckDB.Generator):
        # TRANSFORMS entries win over *_sql methods: drop the ones overridden below.
        TRANSFORMS = {  # noqa: RUF012 - overrides the base class table, as SQLGlot does
            k: v
            for k, v in DuckDB.Generator.TRANSFORMS.items()
            if k not in (exp.Initcap, exp.UnixSeconds, exp.StrToTime, exp.StrToDate)
        }

        def arrayagg_sql(self, expression: exp.ArrayAgg) -> str:
            """ARRAY_AGG([DISTINCT] x [IGNORE NULLS] [ORDER BY ...] [LIMIT n]).

            SQLGlot 30.22 copies `ORDER BY ... LIMIT` into the FILTER it emits for IGNORE NULLS,
            producing invalid SQL, and rejects LIMIT. DuckDB has no LIMIT inside an aggregate, so
            `LIMIT n` becomes list_slice(..., 1, n) over the ordered aggregate.
            TODO(upstream): SQLGlot PR draft in .local/upstream/ (link here once opened).
            """
            node = expression.this
            limit = None
            if isinstance(node, exp.Limit):
                limit, node = node.expression, node.this
            order: list[exp.Expr] = []
            if isinstance(node, exp.Order):
                order, node = list(node.expressions), node.this
            distinct = isinstance(node, exp.Distinct)
            value = node.expressions[0] if isinstance(node, exp.Distinct) else node
            if limit is not None and isinstance(expression.parent, exp.Window):
                raise UnsupportedError("ARRAY_AGG with LIMIT as a window function")
            inner = ("DISTINCT " if distinct else "") + self.sql(value)
            if order:
                inner += " ORDER BY " + ", ".join(self.sql(o) for o in order)
            sql = f"ARRAY_AGG({inner})"
            if expression.args.get("nulls_excluded"):
                sql += f" FILTER(WHERE {self.sql(value)} IS NOT NULL)"
            if limit is not None:
                sql = f"LIST_SLICE({sql}, 1, {self.sql(limit)})"
            return sql

        # SQLGlot 30.22 builds several DuckDB expressions from generated SQL TEXT (exp.case().when
        # with an f-string, exp.cast of self.func(...)), which re-parses it in the default dialect:
        # an array index DuckDB already shifted to 1-based (a[2]) is shifted again (a[3]). The
        # overrides below build the same SQL without re-parsing. TODO(upstream): draft in
        # .local/upstream/.

        def initcap_sql(self, expression: exp.Initcap) -> str:
            """INITCAP(x[, delimiters]) as SQLGlot writes it, assembled as text only."""
            value = self.sql(expression, "this")
            delimiters = expression.args.get("expression")
            if delimiters is None:
                delimiters = exp.Literal.string(self.dialect.INITCAP_DEFAULT_DELIMITER_CHARS)
            escaped = _escape_regex_metachars(self, delimiters, self.sql(delimiters))
            if escaped == "''":
                return f"UPPER(LEFT({value}, 1)) || LOWER(SUBSTRING({value}, 2))"
            first_is_delimiter = f"REGEXP_MATCHES(LEFT({value}, 1), CONCAT('[', {escaped}, ']'))"
            segments = (
                f"REGEXP_EXTRACT_ALL({value}, CONCAT('([', {escaped}, ']+|[^', {escaped}, ']+)'))"
            )

            def capitalize(parity: int) -> str:
                return (
                    f"LIST_TRANSFORM({segments}, (seg, idx) -> CASE WHEN idx % 2 = {parity} "
                    "THEN UPPER(LEFT(seg, 1)) || LOWER(SUBSTRING(seg, 2)) ELSE seg END)"
                )

            return (
                f"ARRAY_TO_STRING(CASE WHEN {first_is_delimiter} THEN {capitalize(0)} "
                f"ELSE {capitalize(1)} END, '')"
            )

        def strtotime_sql(self, expression: exp.StrToTime) -> str:
            """PARSE_TIMESTAMP / SAFE.PARSE_TIMESTAMP with the casts built as trees."""
            target_type = expression.args.get("target_type")
            needs_tz = bool(target_type) and target_type.this in (
                exp.DType.TIMESTAMPLTZ,
                exp.DType.TIMESTAMPTZ,
            )
            value, formatted_time = self._strptime_default_year(expression)
            safe = bool(expression.args.get("safe"))
            call = exp.Anonymous(
                this="TRY_STRPTIME" if safe else "STRPTIME", expressions=[value, formatted_time]
            )
            if safe:
                to = exp.DType.TIMESTAMPTZ if needs_tz else exp.DType.TIMESTAMP
                return self.sql(exp.Cast(this=call, to=exp.DataType(this=to)))
            if needs_tz:
                return self.sql(exp.Cast(this=call, to=exp.DataType(this=exp.DType.TIMESTAMPTZ)))
            return self.sql(call)

        def unixseconds_sql(self, expression: exp.UnixSeconds) -> str:
            epoch = exp.Anonymous(
                this="EPOCH", expressions=[_implicit_datetime_cast(expression.this)]
            )
            return self.sql(exp.Cast(this=epoch, to=exp.DataType(this=exp.DType.BIGINT)))

        def like_sql(self, expression: exp.Like) -> str:
            # BigQuery escapes `%`, `_` and `\` with a backslash; DuckDB's LIKE has no escape
            # character unless one is named.
            pattern = expression.expression
            if isinstance(pattern, exp.Literal) and pattern.is_string:
                trailing = len(pattern.this) - len(pattern.this.rstrip("\\"))
                if trailing % 2:
                    # shortcut: literal patterns only; BigQuery's error for this is unrecorded.
                    self.unsupported("LIKE pattern ending in an unescaped backslash")
            sql = super().like_sql(expression)
            return sql if isinstance(expression.parent, exp.Escape) else f"{sql} ESCAPE '\\'"

        def strtodate_sql(self, expression: exp.StrToDate) -> str:
            """PARSE_DATE -> CAST(STRPTIME(...) AS DATE), built as a tree.

            SQLGlot 30.22 passes the generated STRPTIME text to exp.cast, which re-parses it in the
            default dialect: REGEXP_EXTRACT(x, p, 1) comes back with 1 as a position, and the
            capture group is lost (PARSE_DATE('%Y-%m', REGEXP_EXTRACT(...)) parsed the whole match).
            TODO(upstream): SQLGlot PR draft in .local/upstream/.
            """
            value, formatted_time = self._strptime_default_year(expression)
            name = "TRY_STRPTIME" if expression.args.get("safe") else "STRPTIME"
            call = exp.Anonymous(this=name, expressions=[value, formatted_time])
            return self.sql(exp.Cast(this=call, to=exp.DataType(this=exp.DType.DATE)))


_DIALECT = BigQueryToDuckDB()


def generate(tree: exp.Expr) -> str:
    """DuckDB SQL, every identifier quoted (ADR 0010); SQLGlot's unsupported warnings raise."""
    try:
        return _DIALECT.generate(tree, identify=True, unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as exc:
        raise CrucibleUnsupportedError(str(exc).strip()) from exc
