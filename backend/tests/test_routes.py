"""Tests for FastAPI endpoints (no real Anthropic calls)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from infraguard.main import app


client = TestClient(app)


def test_health_returns_status_and_config_flag():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "anthropic_configured" in body
    assert "github_configured" in body
    assert body["executor"] in ("mock", "github")
    assert "model" in body


def test_scenarios_returns_four_entries():
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    scenarios = resp.json()
    assert len(scenarios) == 4
    ids = {s["id"] for s in scenarios}
    assert ids == {"open-ssh", "missing-tags", "public-s3", "idle-compute"}
    for s in scenarios:
        assert {"id", "label", "description", "severity", "metrics"} <= set(s.keys())


def test_start_run_with_unknown_scenario_returns_404():
    resp = client.post("/runs", json={"scenario_id": "does-not-exist"})
    assert resp.status_code == 404


def test_get_run_unknown_returns_404():
    resp = client.get("/runs/run_unknown")
    assert resp.status_code == 404


def test_approve_unknown_run_returns_503_or_409():
    # Without an API key configured, the runner factory raises 503.
    # With one, an unknown run returns 409. Either is acceptable.
    resp = client.post("/runs/run_does_not_exist/approve")
    assert resp.status_code in (409, 503)


def test_runs_list_starts_empty_or_has_prior_test_runs():
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
