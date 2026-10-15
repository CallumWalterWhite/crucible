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

    INT64 overflow reaches DuckDB as arithmetic overflow ("Overflow in multiplication of INT64")
    or as the projection CAST of a HUGEINT sum; BigQuery says "integer overflow".
    """
    if exc.kind is not ErrorKind.RUNTIME:
        return exc
    first = exc.message.splitlines()[0] if exc.message else ""
    if "Overflow in" in first or ("INT128" in first and "INT64" in first):
        return CrucibleError(
            ErrorKind.RUNTIME, f"integer overflow ({first})", location=exc.location
        )
    return exc
