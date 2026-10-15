from __future__ import annotations

import pytest

from tests.unit.bigquery import bigquery_runner


@pytest.fixture
def bq():
    """run(sql) through the BigQuery dialect; see tests/unit/bigquery.py."""
    yield from bigquery_runner()
