"""Query parameters in SQL: `@name` and positional `?`.

The server parses the request's queryParameters (server/bigquery/params.py) into
session.state[STATE]: parameter name, or 1-based position, -> BigQuery type SQL. Each reference
becomes `CAST($p_<name> AS <type>)` before qualification, so it is typed like any expression and
its value is bound by DuckDB, never written into the SQL.
"""

from __future__ import annotations

from sqlglot import exp

from crucible.dialects.bigquery import errors
from crucible.engine import ExecContext

STATE = "query_parameters"
POSITION = "crucible_parameter_position"  # Placeholder.meta, set by statements.split


def bind(query: exp.Expr, ctx: ExecContext) -> None:
    """Replace each parameter reference in `query` with its typed DuckDB placeholder."""
    declared: dict[str, str] = ctx.session.state.get(STATE) or {}
    for node in list(query.find_all(exp.Parameter, exp.Placeholder)):
        if isinstance(node, exp.Placeholder):
            name = str(node.meta.get(POSITION, ""))
            missing = f"Positional parameter {name} not found" if name else "Missing parameter"
        else:
            name = node.name.lower()
            missing = f"Query parameter '{node.name}' not found"
        if name not in declared:
            raise errors.invalid(missing)
        kind = exp.DataType.build(declared[name], dialect="bigquery")
        node.replace(exp.Cast(this=exp.Placeholder(this=f"p_{name}"), to=kind))
