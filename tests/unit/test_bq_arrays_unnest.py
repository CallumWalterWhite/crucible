"""ARRAY_AGG (IGNORE NULLS, ORDER BY, LIMIT), UNNEST as a relation, and || with literal NULL."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import column_types, values


@pytest.fixture
def data(bq):
    bq("CREATE TABLE t (k STRING, x INT64, y INT64)")
    bq("INSERT INTO t VALUES ('a', 1, 10), ('a', NULL, 30), ('a', 3, 20), ('b', 5, 1)")
    return bq


def test_array_agg_ignore_nulls_order_by_limit(data):
    result = data(
        "SELECT k, ARRAY_AGG(x IGNORE NULLS ORDER BY y DESC LIMIT 1)[SAFE_OFFSET(0)] AS top "
        "FROM t GROUP BY k ORDER BY k"
    )
    assert values(result) == [["a", 3], ["b", 5]]


def test_array_agg_order_by_limit(data):
    result = data("SELECT ARRAY_AGG(x ORDER BY y LIMIT 2) AS a FROM t")
    assert values(result) == [[[5, 1]]]


def test_array_agg_distinct_ignore_nulls(data):
    assert values(data("SELECT ARRAY_AGG(DISTINCT x IGNORE NULLS ORDER BY x) AS a FROM t")) == [
        [[1, 3, 5]]
    ]


def test_array_agg_limit_as_window_function_is_unsupported(data):
    with pytest.raises(CrucibleUnsupportedError):
        data("SELECT ARRAY_AGG(x ORDER BY y LIMIT 1) OVER (PARTITION BY k) AS a FROM t")


@pytest.fixture
def rows(bq):
    bq("CREATE TABLE t (id INT64, a INT64, b INT64, arr ARRAY<STRING>)")
    bq("INSERT INTO t VALUES (1, 5, 0, ['x', 'y']), (2, 7, 9, CAST([] AS ARRAY<STRING>))")
    return bq


def test_unaliased_unnest_of_structs_exposes_fields(rows):
    result = rows(
        "SELECT id, m, v FROM t, UNNEST([STRUCT('a' AS m, a AS v), STRUCT('b' AS m, b AS v)]) "
        "WHERE v > 0 ORDER BY id, m"
    )
    assert values(result) == [[1, "a", 5], [2, "a", 7], [2, "b", 9]]


def test_unnest_with_offset_is_zero_based_int64(rows):
    result = rows("SELECT id, x, o FROM t, UNNEST(t.arr) AS x WITH OFFSET o ORDER BY id, o")
    assert column_types(result) == [("id", "INT64"), ("x", "STRING"), ("o", "INT64")]
    assert values(result) == [[1, "x", 0], [1, "y", 1]]


def test_left_join_unnest_with_offset_keeps_empty_arrays(rows):
    result = rows(
        "SELECT id, x, o FROM t LEFT JOIN UNNEST(t.arr) AS x WITH OFFSET o ORDER BY id, o"
    )
    assert values(result) == [[1, "x", 0], [1, "y", 1], [2, None, None]]


def test_unnest_alias_wins_over_joined_column(bq):
    bq("CREATE TABLE items (id INT64, tags ARRAY<STRING>)")
    bq("INSERT INTO items VALUES (1, ['p', 'q'])")
    bq("CREATE TABLE rank_table (tag STRING, r INT64)")
    bq("INSERT INTO rank_table VALUES ('p', 10), ('q', 20)")
    result = bq(
        "SELECT i.id, tag, rk.r FROM items i LEFT JOIN UNNEST(i.tags) AS tag "
        "LEFT JOIN rank_table rk ON rk.tag = tag ORDER BY tag"
    )
    assert values(result) == [[1, "p", 10], [1, "q", 20]]


def test_concat_operator_rejects_literal_null(bq):
    with pytest.raises(CrucibleError) as info:
        bq("SELECT 'a' || NULL AS v")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert values(bq("SELECT 'a' || CAST(NULL AS STRING) AS v")) == [[None]]
