from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    ("ref_type", "ref_name", "expected_code"),
    [("tag", "v0.1.0a1", 0), ("tag", "v0.2.0", 1), ("branch", "main", 1)],
)
def test_release_requires_a_tag_matching_the_package_version(ref_type, ref_name, expected_code):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("name") == "Validate release tag"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            "GITHUB_REF_TYPE": ref_type,
            "GITHUB_REF_NAME": ref_name,
            "PACKAGE_VERSION": "0.1.0a1",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_code, result.stderr
