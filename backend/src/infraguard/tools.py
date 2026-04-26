"""Custom tool schemas + mock executors.

Phase 2 ships mock implementations so demos never break on missing
GitHub credentials. The Protocol allows a real `GithubToolExecutor`
to drop in later without touching `runner.py`.
"""
from __future__ import annotations

import secrets
from typing import Protocol


# ---------------------------------------------------------------------------
# Tool schemas — passed to client.beta.agents.create(tools=[...])
# ---------------------------------------------------------------------------

REPO_CREATE_BRANCH_AND_COMMIT = {
    "type": "custom",
    "name": "repo_create_branch_and_commit",
    "description": (
        "Create a new branch and commit the proposed Terraform changes. "
        "This does not affect production — it only writes to a feature branch. "
        "Auto-approved (low risk)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "branch_name": {
                "type": "string",
                "description": "Name of the branch to create (e.g. fix/restrict-ssh-ingress)",
            },
            "commit_message": {
                "type": "string",
                "description": "Conventional commit message describing the change",
            },
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Relative paths of files modified in this commit",
            },
        },
        "required": ["branch_name", "commit_message", "files_changed"],
    },
}

REPO_OPEN_PULL_REQUEST = {
    "type": "custom",
    "name": "repo_open_pull_request",
    "description": (
        "Open a pull request from the fix branch into main. "
        "REQUIRES HUMAN APPROVAL — this is a privileged action that can lead to deployment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "branch_name": {"type": "string"},
            "title": {"type": "string", "description": "PR title (under 70 chars)"},
            "body": {
                "type": "string",
                "description": "PR description with summary, root cause, and test plan",
            },
            "risk_level": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
                "description": "Operator-facing risk classification",
            },
        },
        "required": ["branch_name", "title", "body", "risk_level"],
    },
}

CI_GET_LATEST_STATUS = {
    "type": "custom",
    "name": "ci_get_latest_status",
    "description": (
        "Check the latest CI run status for a pull request. "
        "Auto-approved (read-only)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pr_number": {"type": "integer"},
        },
        "required": ["pr_number"],
    },
}

ALL_TOOL_SCHEMAS = [
    REPO_CREATE_BRANCH_AND_COMMIT,
    REPO_OPEN_PULL_REQUEST,
    CI_GET_LATEST_STATUS,
]

# Tools that require explicit human approval before execution.
APPROVAL_REQUIRED_TOOLS = {"repo_open_pull_request"}


# ---------------------------------------------------------------------------
# Executor Protocol + mock implementation
# ---------------------------------------------------------------------------

class ToolExecutor(Protocol):
    async def execute(self, name: str, inputs: dict) -> dict: ...


class MockToolExecutor:
    """Returns realistic-looking results without doing real GitHub work."""

    REPO_URL = "https://github.com/asellers3rd/infraguard-agent"

    async def execute(self, name: str, inputs: dict) -> dict:
        if name == "repo_create_branch_and_commit":
            return {
                "branch": inputs.get("branch_name", "fix/auto-remediate"),
                "commit_sha": secrets.token_hex(4),
                "files_changed": inputs.get("files_changed", []),
                "commit_url": f"{self.REPO_URL}/commit/{secrets.token_hex(4)}",
            }
        if name == "repo_open_pull_request":
            pr_number = secrets.randbelow(60) + 40
            return {
                "pr_number": pr_number,
                "pr_url": f"{self.REPO_URL}/pull/{pr_number}",
                "status": "open",
                "title": inputs.get("title", "Auto-remediation"),
                "risk_level": inputs.get("risk_level", "medium"),
            }
        if name == "ci_get_latest_status":
            return {
                "status": "passed",
                "duration_s": 12,
                "plan_summary": "1 to change, 0 to add, 0 to destroy",
                "checks": [
                    {"name": "terraform-validate", "conclusion": "success"},
                    {"name": "tfsec", "conclusion": "success"},
                    {"name": "infracost", "conclusion": "success"},
                ],
            }
        raise ValueError(f"Unknown tool: {name}")
