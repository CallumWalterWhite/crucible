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
        pass


_DIALECT = BigQueryToDuckDB()


def generate(tree: exp.Expr) -> str:
    """DuckDB SQL, every identifier quoted (ADR 0010); SQLGlot's unsupported warnings raise."""
    try:
        return _DIALECT.generate(tree, identify=True, unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as exc:
        raise CrucibleUnsupportedError(str(exc).strip()) from exc
