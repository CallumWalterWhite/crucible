"""Committed API fixtures must not identify anything.

The rules are the ones `corpus/record_api.py` applies before writing (ADR 0013, "Verification").
`problems(text)` lists every redaction failure in one fixture at once, so a failing run names
them all. The fixtures live in tests/fixtures/bigquery_api, written by `crucible corpus
record-api` (run by the user). Until that directory exists the fixture test is skipped, and a
clean sample in redaction_samples/ keeps the checks exercised.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/bigquery_api"
SAMPLES = pathlib.Path(__file__).resolve().parent / "redaction_samples"

TOKEN_LIKE = re.compile(
    r"ya29\.|Bearer |AIza[0-9A-Za-z_-]{20,}|\"access_token\"|\"refresh_token\"|\"private_key\""
)
# An email address; a bare `@` is allowed (query parameters are `@name` in SQL).
EMAIL_LIKE = re.compile(r"[\w.+%-]+@[\w-]+(\.[\w-]+)+")
# Keys whose values must be placeholders such as <project> or <time>.
PLACEHOLDER_KEYS = {
    "projectId",
    "datasetId",
    "jobId",
    "etag",
    "selfLink",
    "creationTime",
    "startTime",
    "endTime",
    "lastModifiedTime",
    "location",
    "user_email",
    "queryId",
}
PLACEHOLDER = re.compile(r"^<[^<>]+>$")
RESOURCE_KINDS = {"bigquery#dataset", "bigquery#table", "bigquery#job"}
UPLOAD_ID_OK = re.compile(r"upload_id=(?!<upload-id>)")


def _pairs(obj: Any):
    """Yield (container, key, value) for every key/value pair in a parsed JSON document."""
    if isinstance(obj, dict):
        for key, value in obj.items():  # pyright: ignore[reportUnknownVariableType]
            yield obj, str(key), value  # pyright: ignore[reportUnknownArgumentType]
            yield from _pairs(value)
    elif isinstance(obj, list):
        for value in obj:  # pyright: ignore[reportUnknownVariableType]
            yield from _pairs(value)


# One private term per line, git-ignored: real project ids and numbers stay out of the repository.
PRIVATE_TERMS_FILE = ROOT / ".local" / "private_terms.txt"


def _private_terms() -> list[str]:
    raw = os.environ.get("CRUCIBLE_PRIVATE_TERMS", "")
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    if PRIVATE_TERMS_FILE.is_file():
        lines = PRIVATE_TERMS_FILE.read_text(encoding="utf-8").splitlines()
        terms += [t.strip() for t in lines if t.strip() and not t.startswith("#")]
    return terms


def problems(text: str) -> list[str]:
    """Every redaction problem in one fixture's text. An empty list means the fixture is clean."""
    found: list[str] = []
    if EMAIL_LIKE.search(text):
        found.append("contains an email address")
    for match in sorted(set(m.group(0) for m in TOKEN_LIKE.finditer(text))):
        found.append(f"contains token-like text: {match!r}")
    for term in _private_terms():
        if term in text:
            found.append(f"contains a term from CRUCIBLE_PRIVATE_TERMS: {term!r}")
    for match in UPLOAD_ID_OK.finditer(text):
        found.append(f"upload URL has a non-placeholder upload_id at offset {match.start()}")
    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        return [*found, f"not valid JSON: {exc}"]
    resources = [document] + [value for _, _, value in _pairs(document) if isinstance(value, dict)]
    for container, key, value in _pairs(document):
        if key in PLACEHOLDER_KEYS and isinstance(value, str | int | float | bool):
            # An error object's "location" names the failing field, not a region, so it is exempt.
            is_error_field = key == "location" and "reason" in container
            if not is_error_field and not (isinstance(value, str) and PLACEHOLDER.match(value)):
                found.append(f"{key} is not a placeholder: {value!r}")
    for resource in resources:
        if resource.get("kind") in RESOURCE_KINDS and isinstance(resource.get("id"), str):
            resource_id = resource["id"]
            first_segment = resource_id.split(":", 1)[0]
            if (":" in resource_id or "." in resource_id) and first_segment != "<project>":
                found.append(f"resource id is not under <project>: {resource_id!r}")
    return found


def _fixture_paths() -> list[pathlib.Path]:
    return sorted(FIXTURES.glob("*.json")) if FIXTURES.is_dir() else []


# --- the checks themselves -----------------------------------------------------------------------


def test_clean_sample_passes():
    samples = sorted(SAMPLES.glob("*.json"))
    assert samples, "redaction_samples/ must hold at least one clean fixture"
    for path in samples:
        assert problems(path.read_text(encoding="utf-8")) == [], path.name


@pytest.mark.parametrize(
    "bad",
    [
        '{"user": "someone@example.com"}',
        '{"member": "user:a.b+c@mail.example.org"}',
        '{"authorization": "Bearer abc.def"}',
        '{"token": "ya29.a0AfH6SMB"}',
        '{"key": "AIzaSyD-1234567890abcdefghijk"}',
        '{"access_token": "x"}',
        '{"refresh_token": "x"}',
        '{"private_key": "x"}',
    ],
)
def test_token_like_and_email_text_is_reported(bad):
    assert problems(bad), bad


def test_a_query_parameter_reference_is_not_an_email():
    assert problems(json.dumps({"query": "SELECT @x AS v, @st.y AS y"})) == []


def test_non_placeholder_project_id_is_reported():
    text = json.dumps({"jobReference": {"projectId": "example-project", "jobId": "<job-1>"}})
    found = problems(text)
    assert any("projectId is not a placeholder" in p for p in found), found


def test_placeholder_values_pass():
    text = json.dumps(
        {"projectId": "<project>", "jobId": "<job-1>", "etag": "<etag>", "location": "<location>"}
    )
    assert problems(text) == []


def test_error_object_location_is_allowed():
    error = {
        "error": {
            "code": 400,
            "message": "x",
            "errors": [{"reason": "invalidQuery", "location": "query"}],
        }
    }
    assert problems(json.dumps(error)) == []


def test_resource_id_must_start_with_project_placeholder():
    text = json.dumps({"kind": "bigquery#table", "id": "example-project:analytics.t1"})
    found = problems(text)
    assert any("not under <project>" in p for p in found), found


def test_resource_id_with_placeholder_project_passes():
    text = json.dumps({"kind": "bigquery#table", "id": "<project>:<dataset>.<table-1>"})
    assert problems(text) == []


def test_upload_id_must_be_placeholder():
    assert problems('{"url": "https://x/upload?upload_id=<upload-id>"}') == []
    found = problems('{"url": "https://x/upload?upload_id=AD-real-value"}')
    assert found, "a real upload_id must be reported"


def test_private_terms_are_reported_when_set(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_PRIVATE_TERMS", "zebra-project, ,yak-dataset")
    found = problems('{"note": "zebra-project and yak-dataset"}')
    assert any("zebra-project" in p for p in found), found
    assert any("yak-dataset" in p for p in found), found
    monkeypatch.delenv("CRUCIBLE_PRIVATE_TERMS")
    assert problems('{"note": "zebra-project"}') == []


# --- every committed fixture ---------------------------------------------------------------------


def test_fixture_directory_exists_or_skip():
    if not FIXTURES.is_dir():
        pytest.skip(
            f"no fixtures yet: {FIXTURES.relative_to(ROOT)} is created by "
            "`crucible corpus record-api`"
        )
    assert _fixture_paths(), f"{FIXTURES.relative_to(ROOT)} exists but holds no .json fixtures"


_NO_FIXTURES = pytest.param(
    None, marks=pytest.mark.skip(reason="no fixtures yet (see above)"), id="none"
)


@pytest.mark.parametrize(
    "path", _fixture_paths() or [_NO_FIXTURES], ids=lambda p: getattr(p, "name", "none")
)
def test_fixture_has_no_problems(path):
    if path is None:
        pytest.skip("no fixtures yet")
    found = problems(path.read_text(encoding="utf-8"))
    assert found == [], f"{path.name}:\n  " + "\n  ".join(found)
