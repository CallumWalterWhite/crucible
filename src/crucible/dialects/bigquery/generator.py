"""DuckDB generation for ASTs parsed from BigQuery SQL (ADR 0008).

Fixes for how a BigQuery construct must be written in DuckDB override generator methods here. This
class is never used for another source dialect.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.duckdb import DuckDB
from sqlglot.errors import ErrorLevel, UnsupportedError

from crucible.engine import CrucibleUnsupportedError


class BigQueryToDuckDB(DuckDB):
    class Generator(DuckDB.Generator):
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
