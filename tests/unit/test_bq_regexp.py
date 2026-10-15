"""REGEXP_EXTRACT and REGEXP_SUBSTR: NULL on no match, '' on empty match (transforms/regexp.py)."""

from __future__ import annotations

import pytest

from crucible.engine import CrucibleError, CrucibleUnsupportedError, ErrorKind
from tests.unit.bigquery import column_types, values


def test_no_match_is_null(bq):
    result = bq("SELECT REGEXP_EXTRACT('abc', r'[0-9]') AS v")
    assert column_types(result) == [("v", "STRING")]
    assert values(result) == [[None]]


def test_empty_match_is_an_empty_string(bq):
    # The pattern matches the empty string, so the result is '' and not NULL.
    assert values(bq("SELECT REGEXP_EXTRACT('abc', 'x*') AS v")) == [[""]]


def test_whole_match(bq):
    assert values(bq("SELECT REGEXP_EXTRACT('ab12cd', r'[0-9]+') AS v")) == [["12"]]


def test_one_capturing_group_returns_the_match_of_the_pattern(bq):
    assert values(bq("SELECT REGEXP_EXTRACT('ab12cd', r'b([0-9])') AS v")) == [["1"]]


def test_non_capturing_group_is_allowed(bq):
    assert values(bq("SELECT REGEXP_EXTRACT('ab12', r'(?:b)([0-9])') AS v")) == [["1"]]


def test_null_subject_is_null(bq):
    assert values(bq("SELECT REGEXP_EXTRACT(CAST(NULL AS STRING), r'b') AS v")) == [[None]]


def test_regexp_substr_is_the_same_function(bq):
    assert values(bq("SELECT REGEXP_SUBSTR('abc', r'[0-9]') AS v")) == [[None]]
    assert values(bq("SELECT REGEXP_SUBSTR('ab12', r'[0-9]+') AS v")) == [["12"]]


def test_two_capturing_groups_are_an_invalid_query(bq):
    with pytest.raises(CrucibleError) as info:
        bq("SELECT REGEXP_EXTRACT('ab12', r'(b)([0-9])') AS v")
    assert info.value.kind is ErrorKind.INVALID_QUERY
    assert "must not have more than 1 capturing group" in info.value.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT REGEXP_EXTRACT('abc', r'[0-9]', 2) AS v",
        "SELECT REGEXP_EXTRACT('abc', r'[0-9]', 1, 2) AS v",
    ],
)
def test_position_or_occurrence_is_unsupported(bq, sql):
    with pytest.raises(CrucibleUnsupportedError, match="position/occurrence"):
        bq(sql)


def test_a_pattern_python_cannot_parse_is_unsupported(bq):
    # The group count cannot be checked, so the call is refused rather than guessed.
    with pytest.raises(CrucibleUnsupportedError, match="cannot parse"):
        bq("SELECT REGEXP_EXTRACT('abc', r'(') AS v")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT REGEXP_EXTRACT('abc', CONCAT('a', 'b')) AS v",
        "SELECT REGEXP_SUBSTR('abc', CONCAT('a', 'b')) AS v",
    ],
)
def test_a_non_literal_pattern_is_unsupported(bq, sql):
    # The one-capturing-group rule needs the pattern's text, so a computed pattern is refused.
    with pytest.raises(CrucibleUnsupportedError, match="REGEXP_EXTRACT with a non-literal pattern"):
        bq(sql)


def test_a_null_pattern_is_a_non_literal_and_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match="non-literal pattern"):
        bq("SELECT REGEXP_EXTRACT('abc', CAST(NULL AS STRING)) AS v")


def test_a_pattern_from_a_column_is_unsupported(bq):
    bq("CREATE TABLE t (s STRING, p STRING)")
    bq("INSERT INTO t VALUES ('ab12', '[0-9]+')")
    with pytest.raises(CrucibleUnsupportedError, match="non-literal pattern"):
        bq("SELECT REGEXP_EXTRACT(s, p) AS v FROM t")
