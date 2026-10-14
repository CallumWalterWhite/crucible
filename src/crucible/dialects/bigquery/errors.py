"""BigQuery-style error messages for errors Crucible detects itself."""

from __future__ import annotations

from crucible.engine import CrucibleError, ErrorKind


def table_not_found(project: str, dataset: str, table: str) -> CrucibleError:
    return CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Table {project}:{dataset}.{table}")


def dataset_not_found(project: str, dataset: str) -> CrucibleError:
    return CrucibleError(ErrorKind.NOT_FOUND, f"Not found: Dataset {project}:{dataset}")


def invalid(message: str) -> CrucibleError:
    return CrucibleError(ErrorKind.INVALID_QUERY, message)
