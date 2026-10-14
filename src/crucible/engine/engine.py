"""DuckDB lifecycle, ATTACHed catalogs, cursor per request, hardening (ADRs 0003, 0005, 0006)."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Generator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb

from crucible.engine.catalog import META, Catalog, quote
from crucible.engine.session import Session

_META_DDL = (
    f"create table if not exists {META}.main.catalogs (name varchar primary key, file varchar)",
    f"create table if not exists {META}.main.objects "
    "(catalog varchar, schema varchar, name varchar, properties varchar)",
)


class Engine:
    """Owns the one DuckDB database of this process. Thread-safe; each request gets a cursor.

    database=None keeps everything in memory. Otherwise `database` is the metadata file and each
    catalog lives in `<database>.catalogs/<n>.duckdb`; only that directory is reachable from SQL.
    """

    def __init__(self, database: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(database).resolve() if database is not None else None
        self._catalog_dir = (
            self._path.with_name(self._path.name + ".catalogs") if self._path else None
        )
        self._con = duckdb.connect(
            ":memory:",
            config={"autoinstall_known_extensions": False, "autoload_known_extensions": False},
        )
        self._con.execute(
            f"attach {_literal(str(self._path) if self._path else ':memory:')} as {META}"
        )
        if self._catalog_dir is not None:
            self._catalog_dir.mkdir(parents=True, exist_ok=True)
            self._con.execute(
                f"set allowed_directories = [{_literal(str(self._catalog_dir) + '/')}]"
            )
        self._con.execute("set enable_external_access = false")
        # Neutral base; each dialect applies its session timezone in translation (ADR 0012).
        self._con.execute("set global TimeZone = 'UTC'")
        self._con.execute("set lock_configuration = true")
        for ddl in _META_DDL:
            self._con.execute(ddl)
        self._attached: set[str] = set()
        for name, file in self._con.execute(
            f"select name, file from {META}.main.catalogs"
        ).fetchall():
            self._attach(name, file)

    # --- catalogs ---------------------------------------------------------------------------
    def attached_catalogs(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._attached)

    def attach_catalog(self, name: str) -> None:
        with self._lock:
            if name in self._attached:
                return
            file = None
            if self._catalog_dir is not None:
                count = self._con.execute(f"select count(*) from {META}.main.catalogs").fetchone()
                file = str(self._catalog_dir / f"{count[0] if count else 0}.duckdb")
            self._attach(name, file)
            self._con.execute(f"insert into {META}.main.catalogs values (?, ?)", [name, file])

    def _attach(self, name: str, file: str | None) -> None:
        self._con.execute(f"attach {_literal(file or ':memory:')} as {quote(name)}")
        self._attached.add(name)

    # --- requests ---------------------------------------------------------------------------
    @contextmanager
    def cursor(self, session: Session) -> Generator[duckdb.DuckDBPyConnection]:
        """The session's transaction cursor if one is open, else a fresh cursor for this request."""
        with session.lock:
            cursor = session.transaction or self._con.cursor()
            try:
                yield cursor
            finally:
                if session.transaction is not cursor:
                    cursor.close()

    def catalog(self, cursor: duckdb.DuckDBPyConnection) -> Catalog:
        return Catalog(self, cursor)

    # --- admin ------------------------------------------------------------------------------
    def reset(self) -> None:
        """Drop every catalog and all metadata. Callers must also drop their sessions."""
        with self._lock:
            for name in list(self._attached):
                self._con.execute(f"detach {quote(name)}")
            self._attached.clear()
            self._con.execute(f"delete from {META}.main.catalogs")
            self._con.execute(f"delete from {META}.main.objects")
            if self._catalog_dir is not None:
                for child in self._catalog_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "storage": str(self._path) if self._path else "memory",
                "catalogs": len(self._attached),
                "duckdb": duckdb.__version__,
                "sqlglot": version("sqlglot"),
            }

    def close(self) -> None:
        with self._lock:
            self._con.close()


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
