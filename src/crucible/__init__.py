"""Crucible: a local BigQuery and Snowflake API emulator backed by DuckDB."""

from crucible.engine import CrucibleError, CrucibleUnsupportedError, Engine, Session

__all__ = ["CrucibleError", "CrucibleUnsupportedError", "Engine", "Session"]
