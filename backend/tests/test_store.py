"""Tests for the in-memory run store."""
from __future__ import annotations

import pytest

from infraguard.store import RunEvent, RunStore, ToolRequest, event_to_dict


@pytest.mark.asyncio
async def test_create_and_get_run():
    store = RunStore()
    run = await store.create_run("run_abc", "open-ssh", "Open SSH Ingress")
    assert run.id == "run_abc"
    assert run.status == "pending"
    assert run.events == []
    assert store.get_run("run_abc") is run


@pytest.mark.asyncio
async def test_status_transitions():
    store = RunStore()
    await store.create_run("run_xyz", "missing-tags", "Missing Tags")
    await store.update_status("run_xyz", "running")
    assert store.get_run("run_xyz").status == "running"
    await store.update_status("run_xyz", "completed")
    run = store.get_run("run_xyz")
    assert run.status == "completed"
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_append_event_pushes_to_queue():
    store = RunStore()
    await store.create_run("run_q", "public-s3", "Public S3")
    queue = store.get_queue("run_q")

    event = RunEvent(
        id="evt_1",
        timestamp="2026-04-25T18:00:00Z",
        type="signal_received",
        message="trigger",
    )
    await store.append_event("run_q", event)

    received = await queue.get()
    assert received is event


@pytest.mark.asyncio
async def test_close_stream_pushes_sentinel():
    store = RunStore()
    await store.create_run("run_close", "idle-compute", "Idle Compute")
    queue = store.get_queue("run_close")
    await store.close_stream("run_close")
    assert (await queue.get()) is None


def test_event_to_dict_with_tool_request():
    tr = ToolRequest(
        tool_name="repo_open_pull_request",
        risk_level="high",
        target_repo="acme/repo",
        summary="Test PR",
        parameters={},
        event_id="ev_1",
    )
    event = RunEvent(
        id="evt_2",
        timestamp="2026-04-25T18:00:00Z",
        type="requires_action",
        message="approve please",
        tool_request=tr,
    )
    d = event_to_dict(event)
    assert d["toolRequest"]["toolName"] == "repo_open_pull_request"
    assert d["toolRequest"]["riskLevel"] == "high"
    assert d["toolRequest"]["targetRepo"] == "acme/repo"
