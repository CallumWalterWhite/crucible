"""BigQuery-style error messages for errors Crucible detects itself."""

from __future__ import annotations

from crucible.engine import CrucibleError, ErrorKind


def table_not_found(project: str, dataset: str, table: str) -> CrucibleError:
    return CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Table {project}:{dataset}.{table}")


def dataset_not_found(project: str, dataset: str) -> CrucibleError:
    return CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Dataset {project}:{dataset}")


def invalid(message: str) -> CrucibleError:
    return CrucibleError(ErrorKind.INVALID_QUERY, message)


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
        # A REQUIRED column. BigQuery's wording is unverified (no recording yet): shortcut: the
        # text is a guess that keeps the word NULL; upgrade when the REQUIRED case is recorded.
        message = f"Column {first.rsplit('.', 1)[-1]} cannot be NULL"
    else:
        return exc
    return CrucibleError(ErrorKind.RUNTIME, message, location=exc.location)
