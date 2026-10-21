"""UPDATE, DELETE and TRUNCATE TABLE. UPDATE and DELETE run as a MERGE (dml.py)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import values


@pytest.fixture
def tables(bq):
    bq("CREATE TABLE t (k INT64, a INT64, f FLOAT64)")
    bq("INSERT INTO t VALUES (1, 10, 1.5), (2, 20, 2.5), (3, 30, 3.5)")
    bq("CREATE TABLE s (k INT64, v INT64)")
    bq("INSERT INTO s VALUES (1, 100), (1, 101), (2, 200)")
    return bq


def rows(bq) -> list[list[object]]:
    return values(bq("SELECT k, a, f FROM t ORDER BY k"))


def test_update_changes_matching_rows_and_reports_them(tables):
    result = tables("UPDATE t SET a = a + 1, f = 7 WHERE k >= 2")
    assert (result.statement_type, result.rowcount) == ("UPDATE", 2)
    assert rows(tables) == [[1, 10, 1.5], [2, 21, 7.0], [3, 31, 7.0]]


def test_update_from_a_source_table(tables):
    assert tables("UPDATE t SET a = s.v FROM s WHERE t.k = s.k AND s.k = 2").rowcount == 1
    assert tables("UPDATE t x SET a = y.v FROM s y WHERE x.k = y.k AND y.v = 100").rowcount == 1
    assert rows(tables) == [[1, 100, 1.5], [2, 200, 2.5], [3, 30, 3.5]]


def test_update_matching_two_source_rows_raises_and_changes_nothing(tables):
    with pytest.raises(CrucibleError) as info:
        tables("UPDATE t SET a = s.v FROM s WHERE t.k = s.k")
    assert info.value.kind is ErrorKind.RUNTIME
    assert "must match at most one source row" in info.value.message
    assert rows(tables)[0] == [1, 10, 1.5]


@pytest.mark.parametrize("sql", ["UPDATE t SET a = 1", "DELETE FROM t"])
def test_update_and_delete_need_a_where_clause(tables, sql):
    with pytest.raises(CrucibleError) as info:
        tables(sql)
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert "must have a WHERE clause" in info.value.message


def test_update_checks_the_assigned_type(tables):
    with pytest.raises(CrucibleError) as info:
        tables("UPDATE t SET a = 'x' WHERE TRUE")
    assert info.value.kind is ErrorKind.INVALID_QUERY


def test_delete_removes_matching_rows(tables):
    result = tables("DELETE FROM t WHERE k = 3")
    assert (result.statement_type, result.rowcount) == ("DELETE", 1)
    assert tables("DELETE t WHERE k = 99").rowcount == 0
    assert [r[0] for r in rows(tables)] == [1, 2]


def test_truncate_keeps_the_table(tables):
    result = tables("TRUNCATE TABLE t")
    assert (result.statement_type, result.rowcount) == ("TRUNCATE_TABLE", 3)
    assert rows(tables) == []
    tables("INSERT INTO t VALUES (4, 40, 4.5)")
    assert rows(tables) == [[4, 40, 4.5]]


@pytest.mark.parametrize(
    "sql", ["DELETE FROM v WHERE TRUE", "UPDATE v SET x = 2 WHERE TRUE", "TRUNCATE TABLE v"]
)
def test_dml_on_a_view_is_unsupported(bq, sql):
    bq("CREATE VIEW v AS SELECT 1 AS x")
    with pytest.raises(CrucibleUnsupportedError):
        bq(sql)


def test_merge_names_an_unaliased_source_by_its_table(tables):
    tables("MERGE t USING s ON t.k = s.k AND s.v = 200 WHEN MATCHED THEN UPDATE SET a = s.v")
    assert rows(tables)[1] == [2, 200, 2.5]
