"""BigQuery-style error messages for errors Crucible detects itself."""

from __future__ import annotations

from sqlglot import exp

from crucible.engine import CrucibleError, ErrorKind
from crucible.engine.errors import Location


def table_not_found(project: str, dataset: str, table: str) -> CrucibleError:
    return CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Table {project}:{dataset}.{table}")


def dataset_not_found(project: str, dataset: str) -> CrucibleError:
    return CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Dataset {project}:{dataset}")


def invalid(message: str) -> CrucibleError:
    return CrucibleError(ErrorKind.INVALID_QUERY, message)


STATEMENT_START = "crucible_statement_start"  # tree.meta key set by statements.split


def invalid_at_statement(tree: exp.Expr, message: str) -> CrucibleError:
    """An invalid-query error located at the statement's first token, as BigQuery reports it."""
    start = tree.meta.get(STATEMENT_START)
    return CrucibleError(
        ErrorKind.INVALID_QUERY, message, location=start if isinstance(start, Location) else None
    )


def reword(exc: CrucibleError) -> CrucibleError:
    """BigQuery's wording for runtime errors DuckDB raises itself (recorded messages).

    INT64 overflow reaches DuckDB as arithmetic overflow ("Overflow in multiplication of INT64";
    BigQuery: "Integer Overflow") or as the projection CAST of a HUGEINT aggregate (BigQuery, for
    SUM: "Error in SUM aggregation: integer overflow"). An impossible date in PARSE_DATE is
    "Out-of-range datetime field in parsing function".
    """
    if exc.kind is not ErrorKind.RUNTIME:
        return exc
    first = exc.message.splitlines()[0] if exc.message else ""
    if "Overflow in" in first:
        message = f"Integer Overflow ({first})"
    elif "INT128" in first and "INT64" in first:
        message = f"integer overflow ({first})"
    elif first.startswith("Conversion Error: Date out of range"):
        message = f"Out-of-range datetime field in parsing function ({first})"
    elif first.startswith("Constraint Error: NOT NULL constraint failed: "):
        # A REQUIRED column (recorded: jobs_query_null_into_required).
        message = f"Required field {first.rsplit('.', 1)[-1]} cannot be null"
    else:
        return exc
    return CrucibleError(ErrorKind.RUNTIME, message, location=exc.location)
