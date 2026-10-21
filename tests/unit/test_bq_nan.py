"""NaN rules for FLOAT64 (transforms/nan.py): refusals for comparisons Crucible cannot prove."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleUnsupportedError
from tests.unit.bigquery import values

NAN = "CAST('nan' AS FLOAT64)"


def test_comparisons_with_nan_follow_ieee(bq):
    sql = f"SELECT {NAN} = {NAN}, {NAN} < 1.0, {NAN} >= 1.0, {NAN} != 1.0, 1.0 = 1.0"
    assert values(bq(sql)) == [[False, False, False, True, True]]


def test_a_nan_join_key_matches_nothing(bq):
    bq("CREATE TABLE a (x FLOAT64)")
    bq(f"INSERT INTO a VALUES ({NAN}), (1.0)")
    assert values(bq("SELECT COUNT(*) FROM a AS l JOIN a AS r ON l.x = r.x")) == [[1]]


def test_min_with_nan_is_nan_and_max_too(bq):
    sql = f"SELECT MIN(x), MAX(x) FROM UNNEST([2.0, {NAN}, 1.0]) AS x"
    assert values(bq(sql)) == [["NaN", "NaN"]]


def test_integer_comparisons_are_unchanged(bq):
    assert values(bq("SELECT 1 < 2, 2 IN (1, 2), 3 BETWEEN 1 AND 2")) == [[True, True, False]]


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT {NAN} IS DISTINCT FROM {NAN}",
        f"SELECT NULLIF({NAN}, {NAN})",
        f"SELECT CASE {NAN} WHEN 1.0 THEN 'a' END",
        f"SELECT x IN (SELECT 1.0) FROM UNNEST([{NAN}]) AS x",
        "SELECT SUM(x) OVER (ORDER BY x RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) "
        "FROM UNNEST([1.0]) AS x",
    ],
)
def test_unproven_float_comparisons_are_unsupported(bq, sql):
    with pytest.raises(CrucibleUnsupportedError):
        bq(sql)
