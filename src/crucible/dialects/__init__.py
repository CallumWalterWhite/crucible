"""Dialect registry: each dialect exposes `DIALECT` in `crucible.dialects.<name>.dialect`."""

from __future__ import annotations

import importlib

from crucible.dialects.base import Dialect
from crucible.engine import CrucibleUnsupportedError

NAMES = ("bigquery", "snowflake")


def get_dialect(name: str) -> Dialect:
    if name not in NAMES:
        raise ValueError(f"unknown dialect {name!r}; expected one of {NAMES}")
    try:
        module = importlib.import_module(f"crucible.dialects.{name}.dialect")
    except ModuleNotFoundError as exc:
        if exc.name != f"crucible.dialects.{name}.dialect":
            raise
        raise CrucibleUnsupportedError(f"the {name} dialect (not built yet)") from exc
    dialect: Dialect = module.DIALECT
    return dialect
