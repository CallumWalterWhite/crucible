"""Engine kernel tests with a test-only passthrough dialect (DuckDB SQL in, plans out)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from crucible.engine import (
    CrucibleError,
    Engine,
    ErrorKind,
    Pipeline,
    Result,
    Session,
)
from crucible.engine.errors import RUNTIME_ERROR_PREFIX, UNSUPPORTED_ERROR_PREFIX
from tests.fakes import PassthroughDialect


@pytest.fixture
def engine():
    e = Engine()
    yield e
    e.close()


def run(engine: Engine, sql: str, session: Session | None = None, params=None) -> list[Result]:
    return Pipeline(engine, PassthroughDialect()).execute(session or Session(), sql, params)


def setup_table(engine: Engine) -> None:
    with engine.cursor(Session()) as cur:
        catalog = engine.catalog(cur)
        catalog.ensure_schema("my-project", "ds")
    run(
        engine,
        'create table "my-project".ds.t (a bigint, b varchar); '
        "insert into \"my-project\".ds.t values (1, 'x'), (2, 'y')",
    )


def test_select_returns_columns_types_rows(engine):
    [r] = run(engine, "select 1::bigint as a, [1, 2] as b, {'k': 'v'} as c")
    assert [c.name for c in r.columns] == ["a", "b", "c"]
    assert [c.type.sql("duckdb") for c in r.columns] == ["BIGINT", "INT[]", "STRUCT(k TEXT)"]
    assert r.rows == [(1, [1, 2], {"k": "v"})]


def test_dml_rowcount_and_ddl(engine):
    setup_table(engine)
    [r] = run(engine, "update \"my-project\".ds.t set b = 'z'")
    assert r.rowcount == 2
    assert r.statement_type == "UPDATE"


def test_params(engine):
    [r] = run(engine, "select $x + 1 as y", params={"x": 41})
    assert r.rows == [(42,)]


def test_catalog_mapping_and_metadata(engine):
    setup_table(engine)
    with engine.cursor(Session()) as cur:
        catalog = engine.catalog(cur)
        assert catalog.catalogs() == ["my-project"]
        assert catalog.schemas("my-project") == ["ds"]  # DuckDB's main schema is internal
        info = catalog.table("my-project", "DS", "T")  # DuckDB lookup is case-insensitive
        assert info is not None
        assert (info.schema, info.name, info.kind) == ("ds", "t", "table")
        assert [(c.name, c.type.sql("duckdb")) for c in info.columns] == [
            ("a", "BIGINT"),
            ("b", "TEXT"),
        ]
        catalog.set_properties("my-project", "ds", "t", {"partition": "a"})
        assert catalog.table("my-project", "ds", "t").properties == {"partition": "a"}
        assert [t.name for t in catalog.tables("my-project", "ds")] == ["t"]
        assert catalog.table("my-project", "ds", "missing") is None
        assert catalog.table("no-such-project", "ds", "t") is None


def test_reserved_catalog_names(engine):
    with engine.cursor(Session()) as cur, pytest.raises(CrucibleError) as info:
        engine.catalog(cur).ensure_catalog("memory")
    assert info.value.kind is ErrorKind.INVALID_QUERY


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("select * from nope", ErrorKind.NOT_FOUND),
        ("select nope from (select 1)", ErrorKind.INVALID_QUERY),
        ("selec 1", ErrorKind.INVALID_QUERY),
        ("select cast('x' as int)", ErrorKind.RUNTIME),
        (f"select error('{RUNTIME_ERROR_PREFIX}division by zero: 1 / 0')", ErrorKind.RUNTIME),
    ],
)
def test_error_classification(engine, sql, kind):
    with pytest.raises(CrucibleError) as info:
        run(engine, sql)
    assert info.value.kind is kind


def test_runtime_error_message_is_the_generated_message(engine):
    with pytest.raises(CrucibleError) as info:
        run(engine, f"select error('{RUNTIME_ERROR_PREFIX}division by zero: 1 / 0')")
    assert info.value.message == "division by zero: 1 / 0"


@pytest.mark.parametrize(
    "sql",
    [
        "select * from read_csv('/etc/hosts')",
        "copy (select 1) to '/tmp/crucible-should-not-exist.csv'",
        "attach '/tmp/crucible-should-not-exist.duckdb' as x",
        "install httpfs",
        "load httpfs",
        "set enable_external_access = true",
        "set lock_configuration = false",
        "set TimeZone = 'America/Los_Angeles'",
    ],
)
def test_hardening(engine, sql):
    with pytest.raises(CrucibleError):
        run(engine, sql)


def test_every_cursor_is_utc(engine):
    [r] = run(engine, "select current_setting('TimeZone')")
    assert r.rows == [("UTC",)]


def test_transaction_spans_requests_and_conflict_maps(engine):
    setup_table(engine)
    s1, s2 = Session(), Session()
    run(engine, "begin", s1)
    run(engine, "update \"my-project\".ds.t set b = 's1'", s1)
    assert s1.transaction is not None
    run(engine, "begin", s2)
    with pytest.raises(CrucibleError) as info:
        run(engine, "update \"my-project\".ds.t set b = 's2'", s2)
    assert info.value.kind is ErrorKind.CONFLICT
    run(engine, "rollback", s2)
    run(engine, "commit", s1)
    assert s1.transaction is None
    [r] = run(engine, 'select distinct b from "my-project".ds.t')
    assert r.rows == [("s1",)]


def test_concurrent_requests(engine):
    setup_table(engine)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for _ in range(20):
                run(engine, f"insert into \"my-project\".ds.t values ({i}, 'w')")
                run(engine, 'select count(*) from "my-project".ds.t')
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    [r] = run(engine, 'select count(*) from "my-project".ds.t')
    assert r.rows == [(2 + 8 * 20,)]


def test_reset_clears_catalogs(engine):
    setup_table(engine)
    engine.reset()
    with engine.cursor(Session()) as cur:
        assert engine.catalog(cur).catalogs() == []
    with pytest.raises(CrucibleError):
        run(engine, 'select * from "my-project".ds.t')


def test_persistence_round_trip(tmp_path: Path):
    path = tmp_path / "state.duckdb"
    e = Engine(path)
    setup_table(e)
    with e.cursor(Session()) as cur:
        e.catalog(cur).set_properties("my-project", "ds", "t", {"k": 1})
    e.close()
    e = Engine(path)
    [r] = run(e, 'select count(*) from "my-project".ds.t')
    assert r.rows == [(2,)]
    with e.cursor(Session()) as cur:
        assert e.catalog(cur).table("my-project", "ds", "t").properties == {"k": 1}
    # only the catalog directory is reachable from SQL
    with pytest.raises(CrucibleError):
        run(e, f"select * from read_text('{tmp_path}/state.duckdb')")
    e.reset()
    assert list((tmp_path / "state.duckdb.catalogs").iterdir()) == []
    e.close()


def test_health(engine):
    h = engine.health()
    assert h["status"] == "ok"
    assert h["storage"] == "memory"


def test_json_results_stay_text(engine):
    # JSON stays text so a JSON null ('null') is distinct from SQL NULL (None).
    [r] = run(
        engine, """select '{"a": [1, null]}'::json as j, 'null'::json as n, null::json as s"""
    )
    assert r.rows == [('{"a": [1, null]}', "null", None)]


def test_unsupported_error_from_generated_sql(engine):
    with pytest.raises(CrucibleError) as info:
        run(engine, f"select error('{UNSUPPORTED_ERROR_PREFIX}UPPER of a special-cased character')")
    assert info.value.kind is ErrorKind.UNSUPPORTED
    assert "UPPER of a special-cased character" in info.value.message
