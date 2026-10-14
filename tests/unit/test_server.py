from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from crucible.cli import build_parser
from crucible.engine import Engine
from crucible.server.app import create_app


@pytest.fixture(params=["bigquery", "snowflake"])
def client(request):
    engine = Engine()
    with TestClient(create_app(request.param, engine), raise_server_exceptions=False) as c:
        c.dialect = request.param
        yield c
    engine.close()


def test_health(client):
    r = client.get("/_crucible/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["dialect"] == client.dialect


def test_reset(client):
    assert client.post("/_crucible/reset").json() == {"status": "reset"}


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_unknown_endpoint_uses_warehouse_error_shape(client, method):
    r = client.request(method, "/bigquery/v2/projects/p/unknownThing")
    if client.dialect == "bigquery":
        assert r.status_code == 501
        error = r.json()["error"]
        assert error["errors"][0]["reason"] == "notImplemented"
        assert "Not implemented in Crucible" in error["message"]
    else:
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert int(body["code"]) > 0  # the connector parses int(code)
        assert "Not implemented in Crucible" in body["message"]


def test_wrong_method_on_admin_route_uses_warehouse_shape(client):
    r = client.put("/_crucible/health")
    assert "Not implemented in Crucible" in r.text


def test_cli_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOST", raising=False)
    args = build_parser().parse_args(["serve", "--dialect", "bigquery"])
    assert args.host == "127.0.0.1"


def test_cli_host_from_env(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_HOST", "0.0.0.0")
    args = build_parser().parse_args(["serve", "--dialect", "snowflake"])
    assert args.host == "0.0.0.0"
