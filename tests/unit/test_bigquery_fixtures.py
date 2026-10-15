"""Crucible's REST responses against the recorded BigQuery fixtures, field by field.

The recorder that produced `tests/fixtures/bigquery_api/` runs again, unchanged, against an
in-process Crucible server, and redacts Crucible's responses with the same rules. Every fixture is
then compared with its recorded twin. A difference passes only if DIFFERENCES below lists it with a
reason; an entry that no longer matches anything fails too, so the list stays exact.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from crucible.corpus import record_api
from crucible.engine import Engine
from crucible.server.app import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bigquery_api"
PROJECT = "crucible-replay"
MAX_BYTES_BILLED = 104857600  # as recorded

# The CSV files the committed fixtures were recorded with (record_api.py at commit 941942f; the
# recorder's current LOAD_CASES are newer and not recorded yet). Those upload fixtures hold no
# body; later recordings store the CSV in the upload fixture, and the replay uses that instead.
RECORDED_LOAD_CASES = {
    "baseline": "id,b,ts,d,n,f,s\n"
    '1,true,2024-01-02 03:04:05,2024-01-02,1.5,1.5,"quoted, with comma"\n'
    "2,false,2024-01-02 03:04:05.123456,2024-01-02,-0.000000001,-1.5,plain\n",
    "empty_fields": 'id,b,ts,d,n,f,s\n1,,,,,,\n2,true,2024-01-02,2024-01-02,1,1,""\n',
    "bool_t": "id,b,ts,d,n,f,s\n1,T,2024-01-02,2024-01-02,1,1,x\n",
    "bool_1": "id,b,ts,d,n,f,s\n1,1,2024-01-02,2024-01-02,1,1,x\n",
    "bool_yes": "id,b,ts,d,n,f,s\n1,yes,2024-01-02,2024-01-02,1,1,x\n",
    "bool_mixed_case": "id,b,ts,d,n,f,s\n1,False,2024-01-02,2024-01-02,1,1,x\n",
    "ts_t_z": "id,b,ts,d,n,f,s\n1,true,2024-01-02T03:04:05Z,2024-01-02,1,1,x\n",
    "ts_utc_suffix": "id,b,ts,d,n,f,s\n1,true,2024-01-02 03:04:05.123456 UTC,2024-01-02,1,1,x\n",
    "ts_offset": "id,b,ts,d,n,f,s\n1,true,2024-01-02 03:04:05+01:00,2024-01-02,1,1,x\n",
    "ts_epoch": "id,b,ts,d,n,f,s\n1,true,1704164645,2024-01-02,1,1,x\n",
    "date_one_digit": "id,b,ts,d,n,f,s\n1,true,2024-01-02,2024-1-2,1,1,x\n",
    "numeric_exponent": "id,b,ts,d,n,f,s\n1,true,2024-01-02,2024-01-02,1e3,1,x\n",
    "float_inf_nan": "id,b,ts,d,n,f,s\n"
    "1,true,2024-01-02,2024-01-02,1,-inf,x\n2,true,2024-01-02,2024-01-02,1,NaN,y\n",
    "bad_value": "id,b,ts,d,n,f,s\n1,maybe,2024-01-02,2024-01-02,1,1,x\n",
}

# INTERVAL result values are unsupported (DuckDB folds months into days when it returns them to
# Python; docs/compatibility.md), so the replay leaves the `iv` column out of the all-types query.
_INTERVAL_PARTS = (
    (",\n  INTERVAL 1 DAY AS iv\n", "\n"),
    (
        "CAST([] AS ARRAY<STRUCT<k INT64, w STRING>>), NULL\n",
        "CAST([] AS ARRAY<STRUCT<k INT64, w STRING>>)\n",
    ),
    (", INTERVAL -3 MONTH\n", "\n"),
)


def _without_interval(text: str) -> str:
    for old, new in _INTERVAL_PARTS:
        text = text.replace(old, new)
    return text


# Keys Crucible does not emit anywhere, with the reason.
OMITTED_KEYS = {
    "user_email": "no IAM identity (auth is out of scope)",
    "principal_subject": "no IAM identity",
    "access": "dataset ACLs (IAM is out of scope)",
    "maxTimeTravelHours": "time travel is not emulated",
    "queryPlan": "execution details are not emulated",
    "timeline": "execution details are not emulated",
    "queryInfo": "execution details are not emulated",
    "performanceInsights": "execution details are not emulated",
    "metadataCacheStatistics": "execution details are not emulated",
    "reservation_id": "no reservations",
    "totalSlotMs": "slot usage is not emulated",
    "finalExecutionDurationMs": "durations are not emulated",
    "estimatedBytesProcessed": "byte counts are not emulated",
    "totalPartitionsProcessed": "partition pruning is not emulated",
    "billingTier": "billing is not emulated",
    "transferredBytes": "byte counts are not emulated",
    "outputBytes": "storage sizes are not emulated",
    "numBytes": "storage sizes are not emulated",
    "numLongTermBytes": "storage sizes are not emulated",
    "numTotalLogicalBytes": "storage sizes are not emulated",
    "numActiveLogicalBytes": "storage sizes are not emulated",
    "numLongTermLogicalBytes": "storage sizes are not emulated",
    "referencedTables": "not tracked",
    "dmlMode": "an internal execution mode",
    "ddlOperationPerformed": "not tracked (CREATE vs REPLACE)",
    "completionRatio": "progress is not emulated",
}
# Keys present on both sides whose values are measurements, not semantics.
VALUE_FREE_KEYS = {"totalBytesProcessed", "totalBytesBilled", "inputFileBytes"}

_JOBS_RUN_IN_REQUEST = (
    "jobs finish inside the request that creates them (ADR 0004): DONE, not RUNNING"
)
# (fixture name glob, path regex, reason)
DIFFERENCES: list[tuple[str, str, str]] = [
    ("jobs_insert_*", r"^body\.status", _JOBS_RUN_IN_REQUEST),
    (
        "jobs_insert_*",
        r"^body\.statistics\.(endTime|query|totalBytesProcessed)",
        _JOBS_RUN_IN_REQUEST,
    ),
    ("load_*_upload", r"^body\.status", _JOBS_RUN_IN_REQUEST),
    ("load_*_upload", r"^body\.statistics\.(endTime|load)", _JOBS_RUN_IN_REQUEST),
    (
        "tables_get",
        r"^body\.(creationTime|expirationTime|lastModifiedTime)$",
        "object times are not tracked",
    ),
    (
        "tables_patch_add_column",
        r"^body\.(creationTime|expirationTime|lastModifiedTime)$",
        "object times are not tracked",
    ),
    (
        "tables_list",
        r"^body\.tables\[0\]\.(creationTime|expirationTime)$",
        "object times are not tracked",
    ),
    (
        "tables_list_partitioned",
        r"^body\.tables\[\d+\]\.(creationTime|expirationTime)$",
        "object times are not tracked",
    ),
    (
        "tables_get_partitioned_*",
        r"^body\.(creationTime|expirationTime|lastModifiedTime)$",
        "object times are not tracked",
    ),
    (
        "tables_get_anonymous",
        r"^body\.(creationTime|expirationTime|lastModifiedTime)$",
        "object times are not tracked",
    ),
    (
        "*_runtime_error",
        r"^body\.(configuration\.query\.(destinationTable|writeDisposition)|statistics\.query\.(cacheHit|statementType))",
        "a job that fails while running reports no statement type or destination in Crucible",
    ),
    (
        "jobs_query_syntax_error",
        r"^body\.error\.(message|errors\[0\]\.message)$",
        "syntax error wording comes from SQLGlot's parser",
    ),
    (
        "tabledata_list_[ps]*",
        r"^body\.rows",
        "a page of a stored table follows BigQuery's storage order, which it does not define",
    ),
    ("load_*_initiate", r"^headers\.Content-Type$", "content type of an empty body"),
    ("tables_delete", r"^headers\.Content-Type$", "content type of an empty body"),
]


# Calls the recorder makes that the committed fixtures predate (Phase 4, Step A). The replay
# makes them, so Crucible must answer; they are compared once the user records them.
NOT_RECORDED_YET: list[str] = []


STORAGE_ORDER_PAGES = {"tabledata_list", "tabledata_list_int64_timestamp"}


class AppSession:
    """The recorder's HTTP session interface over Starlette's test client."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)
        data = kwargs.pop("data", None)
        response = self.client.request(method, url, content=data, **kwargs)
        # httpx lowercases header names; the recorder keeps a "Location" header only by its case.
        response.headers = {k.title(): v for k, v in response.headers.items()}
        return response


def _load_cases(recorded: dict[str, Any]) -> dict[str, str]:
    """The CSVs to upload: from the upload fixtures when they hold them, else the copy above."""
    uploads = {
        name.removeprefix("load_").removesuffix("_upload"): fixture["request"]["body"]
        for name, fixture in recorded.items()
        if name.startswith("load_") and name.endswith("_upload")
    }
    if uploads and all(isinstance(text, str) for text in uploads.values()):
        return uploads
    return RECORDED_LOAD_CASES


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("replay")
    patch = pytest.MonkeyPatch()
    patch.setattr(record_api, "ALL_TYPES_SQL", _without_interval(record_api.ALL_TYPES_SQL))
    patch.setattr(record_api, "LOAD_CASES", _load_cases(_recorded()))
    patch.setattr(record_api.Recorder, "preflight", lambda self: None)  # a free dry run, no state
    engine = Engine()
    try:
        app = create_app("bigquery", engine)
        with TestClient(app, base_url="https://bigquery.googleapis.com") as client:
            recorder = record_api.Recorder(AppSession(client), PROJECT, MAX_BYTES_BILLED, out)
            recorder.run()
    finally:
        patch.undo()
        engine.close()
    return {_name(p): json.loads(p.read_text()) for p in out.glob("*.json")}


def _name(path: Path) -> str:
    return path.stem.split("_", 1)[1]


def _recorded() -> dict[str, Any]:
    # By call number: past 99, "100_..." sorts before "11_..." as text.
    paths = sorted(FIXTURES.glob("*.json"), key=lambda p: int(p.name.split("_", 1)[0]))
    return {_name(p): json.loads(p.read_text()) for p in paths}


def _normalize_expected(value: Any) -> Any:
    """The recorded side as Crucible's replay would redact it: location words in ids and
    messages became placeholders after the recording (commit da5e4e3), and no `iv` column."""
    if isinstance(value, dict):
        out = {k: _normalize_expected(v) for k, v in value.items()}
        _drop_interval_column(out)
        return out
    if isinstance(value, list):
        return [_normalize_expected(v) for v in value]
    if isinstance(value, str):
        return _without_interval(
            re.sub(r"(?<![A-Za-z0-9_-])US(?![A-Za-z0-9_-])", "<location>", value)
        )
    return value


def _drop_interval_column(body: dict[str, Any]) -> None:
    fields = body.get("schema", {}).get("fields") if isinstance(body.get("schema"), dict) else None
    if not fields:
        return
    names = [f.get("name") for f in fields]
    if "iv" not in names:
        return
    index = names.index("iv")
    body["schema"]["fields"] = [f for f in fields if f.get("name") != "iv"]
    for row in body.get("rows", []):
        row["f"] = [c for i, c in enumerate(row["f"]) if i != index]


def diff(expected: Any, actual: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        found: list[tuple[str, str]] = []
        for key in expected.keys() | actual.keys():
            sub = f"{path}.{key}" if path else key
            if key in OMITTED_KEYS and key not in actual:
                continue
            if key not in actual:
                found.append(
                    (sub, f"missing in Crucible (recorded {json.dumps(expected[key])[:80]})")
                )
            elif key not in expected:
                found.append((sub, f"only in Crucible ({json.dumps(actual[key])[:80]})"))
            elif key in VALUE_FREE_KEYS:
                continue
            else:
                found.extend(diff(expected[key], actual[key], sub))
        return found
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [(path, f"{len(expected)} items recorded, {len(actual)} in Crucible")]
        found = []
        for index, (e, a) in enumerate(zip(expected, actual, strict=True)):
            found.extend(diff(e, a, f"{path}[{index}]"))
        return found
    if expected != actual:
        return [
            (path, f"recorded {json.dumps(expected)[:120]}, Crucible {json.dumps(actual)[:120]}")
        ]
    return []


def _compare(name: str, expected: dict[str, Any], actual: dict[str, Any]) -> list[tuple[str, str]]:
    want = _normalize_expected(expected)
    if name.startswith("tabledata_list"):
        # tabledata.list rows carry no schema: drop the all-types table's `iv` cell by position.
        names = [f["name"] for f in _recorded()["tables_get"]["body"]["schema"]["fields"]]
        if "iv" in names:
            index = names.index("iv")
            for row in want["body"].get("rows", []):
                row["f"] = [c for i, c in enumerate(row["f"]) if i != index]
    if name in STORAGE_ORDER_PAGES:
        # A stored table's tabledata.list order is BigQuery's storage order, which it does not
        # define: compare the whole listing as a multiset.
        for side in (want, actual):
            side["body"]["rows"] = sorted(side["body"].get("rows", []), key=json.dumps)
    found = diff(want["status"], actual["status"], "status")
    found += diff(want["body"], actual["body"], "body")
    # Headers: names are case-insensitive; only Location, Range and Content-Type are recorded.
    want_headers = {k.lower(): v for k, v in want["headers"].items()}
    got_headers = {k.lower(): v for k, v in actual["headers"].items()}
    for key in ("location", "range", "content-type"):
        if key in want_headers and want_headers.get(key) != got_headers.get(key):
            if (
                key == "content-type"
                and "json" in want_headers[key]
                and "json" in got_headers.get(key, "")
            ):
                continue
            label = {"location": "Location", "range": "Range", "content-type": "Content-Type"}[key]
            found.append(
                (
                    f"headers.{label}",
                    f"recorded {want_headers[key]!r}, Crucible {got_headers.get(key)!r}",
                )
            )
    return found


def _listed(name: str, path: str) -> tuple[str, str, str] | None:
    for entry in DIFFERENCES:
        pattern, regex, _ = entry
        if fnmatch.fnmatch(name, pattern) and re.search(regex, path):
            return entry
    return None


def test_every_fixture_matches_or_the_difference_is_listed(replayed):
    recorded = _recorded()
    unexplained: list[str] = []
    used: set[tuple[str, str, str]] = set()
    for name, expected in recorded.items():
        if name not in replayed:
            unexplained.append(f"{name}: Crucible's replay made no such call")
            continue
        for path, problem in _compare(name, expected, replayed[name]):
            entry = _listed(name, path)
            if entry is None:
                unexplained.append(f"{name}: {path}: {problem}")
            else:
                used.add(entry)
    for name in replayed.keys() - recorded.keys():
        if not any(fnmatch.fnmatch(name, pattern) for pattern in NOT_RECORDED_YET):
            unexplained.append(f"{name}: only in Crucible's replay")
    recorded_now = [p for p in NOT_RECORDED_YET if any(fnmatch.fnmatch(n, p) for n in recorded)]
    assert not recorded_now, f"recorded now, remove from NOT_RECORDED_YET: {recorded_now}"
    assert not unexplained, "\n".join(sorted(unexplained))
    stale = [entry for entry in DIFFERENCES if entry not in used]
    assert not stale, f"listed differences that no longer occur: {stale}"
