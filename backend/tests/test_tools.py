"""Tests for tool schemas, the mock executor, and the real GitHub executor."""
from __future__ import annotations

import base64
import json
from typing import Callable

import httpx
import pytest

from infraguard.tools import (
    ALL_TOOL_SCHEMAS,
    APPROVAL_REQUIRED_TOOLS,
    GithubToolExecutor,
    MockToolExecutor,
)


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_all_tool_schemas_have_required_fields():
    for schema in ALL_TOOL_SCHEMAS:
        assert schema["type"] == "custom"
        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"
        assert "required" in schema["input_schema"]


def test_files_changed_schema_requires_path_and_content():
    """Phase 3: agent must emit full file content, not just paths."""
    create_schema = next(
        s for s in ALL_TOOL_SCHEMAS if s["name"] == "repo_create_branch_and_commit"
    )
    items = create_schema["input_schema"]["properties"]["files_changed"]["items"]
    assert items["type"] == "object"
    assert set(items["required"]) == {"path", "content"}


def test_approval_required_tools_are_subset_of_all_tools():
    all_names = {s["name"] for s in ALL_TOOL_SCHEMAS}
    assert APPROVAL_REQUIRED_TOOLS.issubset(all_names)
    assert "repo_open_pull_request" in APPROVAL_REQUIRED_TOOLS
    # Iterative follow-up commits must NOT require approval — the agent should
    # be able to react to CI failures without a human-in-the-loop on every push.
    assert "repo_update_branch" not in APPROVAL_REQUIRED_TOOLS


def test_repo_update_branch_schema_registered():
    names = {s["name"] for s in ALL_TOOL_SCHEMAS}
    assert "repo_update_branch" in names
    schema = next(s for s in ALL_TOOL_SCHEMAS if s["name"] == "repo_update_branch")
    items = schema["input_schema"]["properties"]["files_changed"]["items"]
    assert set(items["required"]) == {"path", "content"}


# ---------------------------------------------------------------------------
# MockToolExecutor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_create_branch_accepts_new_files_changed_shape():
    executor = MockToolExecutor()
    result = await executor.execute(
        "repo_create_branch_and_commit",
        {
            "branch_name": "fix/restrict-ssh",
            "commit_message": "Restrict SSH ingress",
            "files_changed": [
                {"path": "terraform-lab/open-ssh/main.tf", "content": "resource ..."},
            ],
        },
    )
    assert result["branch"] == "fix/restrict-ssh"
    assert result["files_changed"] == ["terraform-lab/open-ssh/main.tf"]
    assert len(result["commit_sha"]) == 8
    assert result["commit_url"].startswith("https://github.com/")


@pytest.mark.asyncio
async def test_mock_create_branch_tolerates_legacy_string_paths():
    """Older agent behavior shipped strings — make sure the mock still copes."""
    executor = MockToolExecutor()
    result = await executor.execute(
        "repo_create_branch_and_commit",
        {
            "branch_name": "fix/foo",
            "commit_message": "x",
            "files_changed": ["main.tf"],
        },
    )
    assert result["files_changed"] == ["main.tf"]


@pytest.mark.asyncio
async def test_mock_update_branch_preserves_branch_name():
    """repo_update_branch must NOT add a suffix — the agent needs to amend the same branch."""
    executor = MockToolExecutor()
    result = await executor.execute(
        "repo_update_branch",
        {
            "branch_name": "fix/restrict-ssh-abc123",
            "commit_message": "Also restrict egress",
            "files_changed": [
                {"path": "open-ssh/main.tf", "content": "resource ..."},
            ],
        },
    )
    assert result["branch"] == "fix/restrict-ssh-abc123"
    assert result["files_changed"] == ["open-ssh/main.tf"]
    assert result["iteration"] is True


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


# ---------------------------------------------------------------------------
# GithubToolExecutor — uses httpx.MockTransport to stub the REST API
# ---------------------------------------------------------------------------


def _github_executor(handler: Callable[[httpx.Request], httpx.Response]) -> GithubToolExecutor:
    return GithubToolExecutor(
        token="ghp_test",
        owner="asellers3rd",
        repo="infraguard-lab",
        default_branch="main",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_github_create_branch_and_commit_happy_path():
    """Resolves the default branch tip, creates a unique branch, commits one file."""
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path

        if path.endswith("/git/refs/heads/main") and request.method == "GET":
            return httpx.Response(
                200, json={"object": {"sha": "base-sha-abc123"}, "ref": "refs/heads/main"}
            )
        if path.endswith("/git/refs") and request.method == "POST":
            body = json.loads(request.content)
            # Branch name should be suffixed for uniqueness
            assert body["ref"].startswith("refs/heads/fix/restrict-ssh-")
            assert body["sha"] == "base-sha-abc123"
            return httpx.Response(201, json={"ref": body["ref"]})
        if "/contents/" in path and request.method == "GET":
            # No existing file
            return httpx.Response(404, json={"message": "Not Found"})
        if "/contents/" in path and request.method == "PUT":
            body = json.loads(request.content)
            # Content is base64 of the proposed file body
            decoded = base64.b64decode(body["content"]).decode("utf-8")
            assert "0.0.0.0/0" not in decoded
            return httpx.Response(
                201,
                json={
                    "commit": {
                        "sha": "commit-sha-deadbeef0000",
                        "message": body["message"],
                    },
                    "content": {"path": "terraform-lab/open-ssh/main.tf"},
                },
            )
        return httpx.Response(500, json={"message": f"unhandled {request.method} {path}"})

    executor = _github_executor(handler)
    try:
        result = await executor.execute(
            "repo_create_branch_and_commit",
            {
                "branch_name": "fix/restrict-ssh",
                "commit_message": "Restrict SSH ingress to VPN CIDR",
                "files_changed": [
                    {
                        "path": "terraform-lab/open-ssh/main.tf",
                        "content": 'resource "aws_security_group" "ssh" {}\n',
                    }
                ],
            },
        )
    finally:
        await executor.aclose()

    assert result["branch"].startswith("fix/restrict-ssh-")
    assert result["files_changed"] == ["terraform-lab/open-ssh/main.tf"]
    assert result["commit_sha"] == "commit-s"  # first 8 chars of "commit-sha-..."
    assert result["commit_url"].endswith("/commit/commit-sha-deadbeef0000")
    # Sanity: we touched all four endpoints
    methods_paths = {(m, p.split("/")[-1]) for m, p in requests}
    assert ("GET", "main") in methods_paths
    assert ("POST", "refs") in methods_paths


@pytest.mark.asyncio
async def test_github_overwrite_existing_file_includes_sha():
    """When the file exists on the branch, the PUT must include its blob sha."""
    seen_put_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/git/refs/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if path.endswith("/git/refs") and request.method == "POST":
            return httpx.Response(201, json={})
        if "/contents/" in path and request.method == "GET":
            return httpx.Response(200, json={"sha": "existing-blob-sha"})
        if "/contents/" in path and request.method == "PUT":
            seen_put_body.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"commit": {"sha": "new-commit-sha"}, "content": {}},
            )
        return httpx.Response(500)

    executor = _github_executor(handler)
    try:
        await executor.execute(
            "repo_create_branch_and_commit",
            {
                "branch_name": "fix/x",
                "commit_message": "update",
                "files_changed": [{"path": "main.tf", "content": "new"}],
            },
        )
    finally:
        await executor.aclose()
    assert seen_put_body["sha"] == "existing-blob-sha"


@pytest.mark.asyncio
async def test_github_update_branch_does_not_create_new_ref():
    """repo_update_branch must amend the existing branch, not POST a new ref."""
    requests: list[tuple[str, str]] = []
    seen_put_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path

        if path.endswith("/git/refs/heads/fix/restrict-ssh-abc123") and request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": "branch-tip-sha"}})
        if "/contents/" in path and request.method == "GET":
            return httpx.Response(200, json={"sha": "existing-blob-sha"})
        if "/contents/" in path and request.method == "PUT":
            seen_put_body.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"commit": {"sha": "iteration-commit-sha"}, "content": {}},
            )
        return httpx.Response(500, json={"message": f"unhandled {request.method} {path}"})

    executor = _github_executor(handler)
    try:
        result = await executor.execute(
            "repo_update_branch",
            {
                "branch_name": "fix/restrict-ssh-abc123",
                "commit_message": "Restrict egress per Trivy AWS-0104",
                "files_changed": [
                    {"path": "open-ssh/main.tf", "content": 'resource "x" {}\n'},
                ],
            },
        )
    finally:
        await executor.aclose()

    # No POST to /git/refs — we must NOT create a sibling branch.
    assert not any(method == "POST" and path.endswith("/git/refs") for method, path in requests)
    # Branch name preserved exactly (no suffix added).
    assert result["branch"] == "fix/restrict-ssh-abc123"
    # PUT carried the existing blob sha so GitHub treats it as an update, not a create.
    assert seen_put_body["sha"] == "existing-blob-sha"
    assert seen_put_body["branch"] == "fix/restrict-ssh-abc123"
    assert result["iteration"] is True
    assert result["files_changed"] == ["open-ssh/main.tf"]


@pytest.mark.asyncio
async def test_github_update_branch_creates_new_file_when_missing():
    """If a follow-up commit adds a file that didn't exist on the branch, no sha is needed."""
    seen_put_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/git/refs/heads/fix/x-deadbe") and request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": "tip"}})
        if "/contents/" in path and request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        if "/contents/" in path and request.method == "PUT":
            seen_put_body.update(json.loads(request.content))
            return httpx.Response(
                201,
                json={"commit": {"sha": "new-commit"}, "content": {}},
            )
        return httpx.Response(500)

    executor = _github_executor(handler)
    try:
        await executor.execute(
            "repo_update_branch",
            {
                "branch_name": "fix/x-deadbe",
                "commit_message": "Add policy file",
                "files_changed": [{"path": "policies/egress.rego", "content": "package x"}],
            },
        )
    finally:
        await executor.aclose()
    # No sha key when the file didn't exist on the branch.
    assert "sha" not in seen_put_body


@pytest.mark.asyncio
async def test_github_open_pull_request():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls") and request.method == "POST":
            body = json.loads(request.content)
            assert body["head"] == "fix/restrict-ssh-abc"
            assert body["base"] == "main"
            return httpx.Response(
                201,
                json={
                    "number": 7,
                    "html_url": "https://github.com/asellers3rd/infraguard-lab/pull/7",
                    "state": "open",
                    "title": body["title"],
                },
            )
        return httpx.Response(500)

    executor = _github_executor(handler)
    try:
        result = await executor.execute(
            "repo_open_pull_request",
            {
                "branch_name": "fix/restrict-ssh-abc",
                "title": "Restrict SSH ingress",
                "body": "Fixes the open-ssh scenario",
                "risk_level": "high",
            },
        )
    finally:
        await executor.aclose()

    assert result["pr_number"] == 7
    assert result["pr_url"].endswith("/pull/7")
    assert result["status"] == "open"
    assert result["risk_level"] == "high"


@pytest.mark.asyncio
async def test_github_ci_status_with_no_checks_returns_queued():
    """Fresh lab repo with no GitHub Actions yet → status reports as queued."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": "head-sha"}})
        if path.endswith("/commits/head-sha/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        return httpx.Response(500)

    executor = _github_executor(handler)
    try:
        result = await executor.execute("ci_get_latest_status", {"pr_number": 7})
    finally:
        await executor.aclose()
    assert result["status"] == "queued"
    assert result["checks"] == []


@pytest.mark.asyncio
async def test_github_ci_status_aggregates_check_runs():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/9"):
            return httpx.Response(200, json={"head": {"sha": "head-sha"}})
        if path.endswith("/commits/head-sha/check-runs"):
            return httpx.Response(
                200,
                json={
                    "total_count": 3,
                    "check_runs": [
                        {"name": "terraform-validate", "status": "completed", "conclusion": "success"},
                        {"name": "tfsec", "status": "completed", "conclusion": "success"},
                        {"name": "infracost", "status": "completed", "conclusion": "success"},
                    ],
                },
            )
        return httpx.Response(500)

    executor = _github_executor(handler)
    try:
        result = await executor.execute("ci_get_latest_status", {"pr_number": 9})
    finally:
        await executor.aclose()
    assert result["status"] == "passed"
    assert {c["name"] for c in result["checks"]} == {
        "terraform-validate",
        "tfsec",
        "infracost",
    }


@pytest.mark.asyncio
async def test_github_ci_status_marks_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/11"):
            return httpx.Response(200, json={"head": {"sha": "head-sha"}})
        if path.endswith("/commits/head-sha/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"name": "tfsec", "status": "completed", "conclusion": "failure"},
                        {"name": "validate", "status": "completed", "conclusion": "success"},
                    ],
                },
            )
        return httpx.Response(500)

    executor = _github_executor(handler)
    try:
        result = await executor.execute("ci_get_latest_status", {"pr_number": 11})
    finally:
        await executor.aclose()
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_github_unknown_tool_raises():
    executor = _github_executor(lambda r: httpx.Response(500))
    try:
        with pytest.raises(ValueError, match="Unknown tool"):
            await executor.execute("nope", {})
    finally:
        await executor.aclose()
