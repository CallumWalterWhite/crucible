"""JSON_TYPE: BigQuery's type names over DuckDB's json_type (transforms/json.py)."""

from __future__ import annotations

import pytest
from sqlglot import parse_one

from crucible.dialects.bigquery.dialect import DIALECT
from crucible.dialects.bigquery.translate import translate_query
from crucible.engine import Engine, ExecContext
from tests.unit.bigquery import DATASET, PROJECT, column_types, values


@pytest.mark.parametrize(
    ("json_text", "expected"),
    [
        ('{"a": 1}', "object"),
        ("[1]", "array"),
        ('"text"', "string"),
        ("1", "number"),
        ("1.5", "number"),
        ("true", "boolean"),
        ("false", "boolean"),
        ("-1", "number"),
        ("18446744073709551615", "number"),
        ("-9223372036854775809", "number"),
    ],
)
def test_each_json_kind(bq, json_text, expected):
    result = bq(f"SELECT JSON_TYPE(PARSE_JSON('{json_text}')) AS v")
    assert column_types(result) == [("v", "STRING")]
    assert values(result) == [[expected]]


def test_json_null_literal_is_null_not_sql_null(bq):
    # A JSON null is the type 'null'; an SQL NULL input gives an SQL NULL.
    assert values(bq("SELECT JSON_TYPE(PARSE_JSON('null')) AS v")) == [["null"]]
    assert values(bq("SELECT JSON_TYPE(CAST(NULL AS JSON)) AS v")) == [[None]]


def test_sql_null_in_a_table_gives_null_per_row(bq):
    bq("CREATE TABLE t (j JSON)")
    bq("INSERT INTO t VALUES (PARSE_JSON('[1]')), (CAST(NULL AS JSON)), (PARSE_JSON('null'))")
    result = bq("SELECT JSON_TYPE(j) AS v FROM t")
    assert sorted(values(result), key=str) == sorted([["array"], [None], ["null"]], key=str)


def test_every_name_duckdb_reports_is_mapped(bq):
    """If DuckDB ever reports a type name this module does not map, the query raises. This
    checks the set of names DuckDB produces for JSON over every kind of number and value."""
    samples = [
        "{}",
        "[]",
        '""',
        "0",
        "-0",
        "1.0",
        "1e308",
        "1e-308",
        "-1.5",
        "9223372036854775807",
        "9223372036854775808",
        "18446744073709551616",
        "-9223372036854775808",
        "170141183460469231731687303715884105727",
        "true",
        "false",
        "null",
    ]
    for sample in samples:
        assert values(bq(f"SELECT JSON_TYPE(PARSE_JSON('{sample}')) AS v")) != [[None]]


def test_unmapped_names_raise_naming_the_json_type():
    """The fallback branch: a name outside the mapping raises CrucibleUnsupportedError with the
    DuckDB name. BigQuery input cannot reach it (see the test above), so the generated SQL is
    checked instead."""
    engine = Engine()
    session = DIALECT.new_session(PROJECT, DATASET)
    with engine.cursor(session) as cursor:
        engine.catalog(cursor).ensure_schema(PROJECT, DATASET)
        ctx = ExecContext(engine, DIALECT, session, cursor, None)
        translation = translate_query(
            parse_one("SELECT JSON_TYPE(PARSE_JSON('[1]')) AS v", read="bigquery"), ctx
        )
    engine.close()
    assert "JSON_TYPE of DuckDB JSON type " in translation.sql
    assert "CASE" in translation.sql
