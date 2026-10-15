"""Warehouse-agnostic errors. Servers map these to each warehouse's native error format."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import duckdb

# Prefixes for errors raised from generated SQL via DuckDB's error() function (ADR 0011): the
# warehouse's own runtime error, or a value Crucible cannot handle exactly (ADR 0009).
RUNTIME_ERROR_PREFIX = "CRUCIBLE:RUNTIME:"
UNSUPPORTED_ERROR_PREFIX = "CRUCIBLE:UNSUPPORTED:"


class ErrorKind(StrEnum):
    INVALID_QUERY = "invalid_query"  # parse/bind/type errors the warehouse reports before running
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    CONFLICT = "conflict"  # concurrent write conflict
    RUNTIME = "runtime"  # errors raised while running: bad cast, division by zero, ...
    UNSUPPORTED = "unsupported"  # valid in the warehouse, not emulated by Crucible
    INTERNAL = "internal"


@dataclass(frozen=True)
class Location:
    """1-based line and column in the client's SQL text."""

    line: int
    column: int


class CrucibleError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, location: Location | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.location = location

    def __str__(self) -> str:
        if self.location is None:
            return self.message
        return f"{self.message} at [{self.location.line}:{self.location.column}]"


class CrucibleUnsupportedError(CrucibleError):
    """A construct the warehouse accepts but Crucible does not emulate (ADR 0009)."""

    def __init__(self, construct: str, *, location: Location | None = None) -> None:
        super().__init__(
            ErrorKind.UNSUPPORTED, f"Not supported by Crucible: {construct}", location=location
        )
        self.construct = construct


def _after(text: str, prefix: str) -> str | None:
    index = text.find(prefix)
    return None if index < 0 else text[index + len(prefix) :]


def runtime_error_message(exc: duckdb.Error) -> str | None:
    """The message of a runtime error raised by generated SQL through error(), else None."""
    return _after(str(exc), RUNTIME_ERROR_PREFIX)


def from_duckdb(exc: duckdb.Error) -> CrucibleError:
    """Classify a DuckDB exception. Dialects reword messages; the kind decides the error class."""
    message = runtime_error_message(exc)
    if message is not None:
        return CrucibleError(ErrorKind.RUNTIME, message)
    construct = _after(str(exc), UNSUPPORTED_ERROR_PREFIX)
    if construct is not None:
        return CrucibleUnsupportedError(construct)
    text = str(exc)
    if isinstance(exc, duckdb.TransactionException) and "Conflict" in text:
        return CrucibleError(ErrorKind.CONFLICT, text)
    if isinstance(exc, duckdb.CatalogException):
        if "already exists" in text:
            return CrucibleError(ErrorKind.ALREADY_EXISTS, text)
        if "does not exist" in text:
            return CrucibleError(ErrorKind.NOT_FOUND, text)
        return CrucibleError(ErrorKind.INVALID_QUERY, text)
    if isinstance(exc, duckdb.ParserException | duckdb.BinderException):
        return CrucibleError(ErrorKind.INVALID_QUERY, text)
    if isinstance(
        exc,
        duckdb.ConversionException
        | duckdb.OutOfRangeException
        | duckdb.ConstraintException
        | duckdb.InvalidInputException,
    ):
        return CrucibleError(ErrorKind.RUNTIME, text)
    return CrucibleError(ErrorKind.INTERNAL, text)
