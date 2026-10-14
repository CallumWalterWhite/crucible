"""The CI guard: only corpus-record/ branches may set `verified: true` (ADR 0013)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check-corpus-verified.sh"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "test")
    case = tmp_path / "corpus" / "bigquery" / "math" / "c.yaml"
    case.parent.mkdir(parents=True)
    case.write_text("query: SELECT 1\nverified: false\nrecorded_at: null\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def set_verified(repo: Path) -> None:
    case = repo / "corpus" / "bigquery" / "math" / "c.yaml"
    case.write_text("query: SELECT 1\nverified: true\nrecorded_at: '2026-01-01T00:00:00Z'\n")
    git(repo, "commit", "-q", "-am", "verify")


def check(repo: Path, branch: str) -> int:
    return subprocess.run(
        ["bash", str(SCRIPT), "main", branch], cwd=repo, capture_output=True
    ).returncode


def test_feature_branch_cannot_set_verified(repo):
    git(repo, "checkout", "-q", "-b", "feature/x")
    set_verified(repo)
    assert check(repo, "feature/x") == 1


def test_record_branch_may_set_verified(repo):
    git(repo, "checkout", "-q", "-b", "corpus-record/bigquery")
    set_verified(repo)
    assert check(repo, "corpus-record/bigquery") == 0


def test_other_changes_pass(repo):
    git(repo, "checkout", "-q", "-b", "feature/y")
    (repo / "README.md").write_text("verified: true\n")  # outside corpus/
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "docs")
    assert check(repo, "feature/y") == 0
