"""Crucible: a local BigQuery API emulator backed by DuckDB. Snowflake is planned."""

from crucible.engine import CrucibleError, CrucibleUnsupportedError, Engine, Session

__all__ = ["CrucibleError", "CrucibleUnsupportedError", "Engine", "Session"]
