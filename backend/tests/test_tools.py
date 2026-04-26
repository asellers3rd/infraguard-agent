"""Tests for tool schemas and the mock executor."""
from __future__ import annotations

import pytest

from infraguard.tools import (
    ALL_TOOL_SCHEMAS,
    APPROVAL_REQUIRED_TOOLS,
    MockToolExecutor,
)


def test_all_tool_schemas_have_required_fields():
    for schema in ALL_TOOL_SCHEMAS:
        assert schema["type"] == "custom"
        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"
        assert "required" in schema["input_schema"]


def test_approval_required_tools_are_subset_of_all_tools():
    all_names = {s["name"] for s in ALL_TOOL_SCHEMAS}
    assert APPROVAL_REQUIRED_TOOLS.issubset(all_names)
    # The PR-opening tool is the privileged one we gate on
    assert "repo_open_pull_request" in APPROVAL_REQUIRED_TOOLS


@pytest.mark.asyncio
async def test_mock_create_branch_returns_expected_shape():
    executor = MockToolExecutor()
    result = await executor.execute(
        "repo_create_branch_and_commit",
        {
            "branch_name": "fix/restrict-ssh",
            "commit_message": "Restrict SSH ingress",
            "files_changed": ["main.tf"],
        },
    )
    assert result["branch"] == "fix/restrict-ssh"
    assert "commit_sha" in result
    assert len(result["commit_sha"]) == 8
    assert result["files_changed"] == ["main.tf"]
    assert result["commit_url"].startswith("https://github.com/")


@pytest.mark.asyncio
async def test_mock_open_pr_returns_expected_shape():
    executor = MockToolExecutor()
    result = await executor.execute(
        "repo_open_pull_request",
        {
            "branch_name": "fix/restrict-ssh",
            "title": "Restrict SSH ingress to VPN CIDR",
            "body": "Fixes open SSH ingress finding",
            "risk_level": "high",
        },
    )
    assert isinstance(result["pr_number"], int)
    assert 40 <= result["pr_number"] <= 99
    assert result["pr_url"].endswith(f"/pull/{result['pr_number']}")
    assert result["status"] == "open"
    assert result["risk_level"] == "high"


@pytest.mark.asyncio
async def test_mock_ci_status_returns_passed():
    executor = MockToolExecutor()
    result = await executor.execute("ci_get_latest_status", {"pr_number": 42})
    assert result["status"] == "passed"
    assert result["duration_s"] > 0
    assert "plan_summary" in result
    assert len(result["checks"]) == 3


@pytest.mark.asyncio
async def test_unknown_tool_raises():
    executor = MockToolExecutor()
    with pytest.raises(ValueError, match="Unknown tool"):
        await executor.execute("nonexistent_tool", {})
