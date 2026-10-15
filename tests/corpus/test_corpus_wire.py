"""Every corpus case over the wire: google-cloud-bigquery against a live Crucible server, through
the recorder's client path (`crucible corpus run --wire`). A case that passes in the library must
pass here too; known divergences fail in both (Phase 3 gate)."""

# ruff: noqa: E402 - the client imports follow the importorskip guards

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

bigquery = pytest.importorskip("google.cloud.bigquery")
uvicorn = pytest.importorskip("uvicorn")

from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials

from crucible.corpus.case import (
    Case,
    find_cases,
)
from crucible.corpus.cli import WIRE_MAX_BYTES_BILLED
from crucible.corpus.record import BigQueryRecorder, wire_outcomes
from crucible.dialects import get_dialect
from crucible.engine import Engine
from crucible.server.app import create_app

BIGQUERY_CASES = find_cases(Path(__file__).resolve().parents[2] / "corpus" / "bigquery")
KNOWN_DIVERGENCE = "known-divergence"


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    engine = Engine()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(create_app("bigquery", engine), log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("the test server did not start")
        threading.Event().wait(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=10)
    engine.close()


@pytest.fixture(scope="module")
def recorder(server_url: str) -> BigQueryRecorder:
    client = bigquery.Client(
        project="crucible-wire",
        credentials=AnonymousCredentials(),
        client_options=ClientOptions(api_endpoint=server_url),
    )
    return BigQueryRecorder(client, get_dialect("bigquery"), WIRE_MAX_BYTES_BILLED)


def _param(case: Case):
    marks = (
        [pytest.mark.xfail(strict=True, reason="tagged known-divergence")]
        if KNOWN_DIVERGENCE in case.tags
        else []
    )
    return pytest.param(case, id=f"{case.area}/{case.name}", marks=marks)


@pytest.mark.parametrize("case", [_param(c) for c in BIGQUERY_CASES])
def test_case_over_the_wire(case: Case, recorder: BigQueryRecorder):
    [outcome] = wire_outcomes([case], recorder)
    assert outcome.passed, outcome.detail
