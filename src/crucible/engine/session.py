"""Per-client session state. Dialects keep their own extra state in `state`."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import duckdb


@dataclass(eq=False)
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    default_catalog: str | None = None
    default_schema: str | None = None
    # Applied by the dialect's translation, not by DuckDB (ADR 0012).
    timezone: str = "UTC"
    variables: dict[str, Any] = field(default_factory=dict[str, Any])
    state: dict[str, Any] = field(default_factory=dict[str, Any])
    # Set while an explicit transaction is open: requests then reuse this cursor (ADR 0003).
    transaction: duckdb.DuckDBPyConnection | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
