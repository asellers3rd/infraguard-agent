"""Custom tool schemas + executors.

Two executors implement the `ToolExecutor` Protocol:

- `MockToolExecutor` returns realistic-looking results without touching GitHub.
  Used when no GITHUB_TOKEN is configured so demos work offline.
- `GithubToolExecutor` calls the real GitHub REST API to create branches,
  commit files, open pull requests, and read check-run status. Activated when
  GITHUB_TOKEN is set.

Selection happens in `routes.get_runner()` based on settings.github_configured.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import zipfile
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas — passed to client.beta.agents.create(tools=[...])
# ---------------------------------------------------------------------------

REPO_CREATE_BRANCH_AND_COMMIT = {
    "type": "custom",
    "name": "repo_create_branch_and_commit",
    "description": (
        "Create a new branch from the default branch and commit the proposed Terraform "
        "changes to it. Auto-approved (low risk — does not affect production)."
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
                "description": (
                    "Every file the fix touches. Provide the FULL file content after the "
                    "fix is applied — not a diff. Existing files are overwritten; new "
                    "files are created."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Repo-relative path (e.g. terraform-lab/open-ssh/main.tf)"
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file content after the fix",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "required": ["branch_name", "commit_message", "files_changed"],
    },
}

REPO_UPDATE_BRANCH = {
    "type": "custom",
    "name": "repo_update_branch",
    "description": (
        "Push follow-up commits to an EXISTING branch you created earlier with "
        "repo_create_branch_and_commit. Use this when CI reports failures on the "
        "open PR — it amends the existing branch (and the existing PR auto-picks "
        "up the new commits) rather than creating a sibling branch and a duplicate "
        "PR. Auto-approved (low risk — does not affect production)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "branch_name": {
                "type": "string",
                "description": (
                    "Exact name of the existing branch as returned by "
                    "repo_create_branch_and_commit (including its random suffix)."
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Conventional commit message describing the follow-up fix",
            },
            "files_changed": {
                "type": "array",
                "description": (
                    "Every file the follow-up commit touches. Provide the FULL file "
                    "content after the fix is applied — not a diff."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repo-relative path",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file content after the follow-up fix",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "required": ["branch_name", "commit_message", "files_changed"],
    },
}

REPO_OPEN_PULL_REQUEST = {
    "type": "custom",
    "name": "repo_open_pull_request",
    "description": (
        "Open a pull request from the fix branch into the default branch. "
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
        "Check the latest CI run status for a pull request. When CI has failed and "
        "Trivy uploaded a SARIF artifact, the response includes a `findings` array "
        "with structured details (rule_id, severity, file, line, message) so you can "
        "diagnose without parsing logs. Auto-approved (read-only)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pr_number": {"type": "integer"},
        },
        "required": ["pr_number"],
    },
}

REPO_ACKNOWLEDGE_FINDING = {
    "type": "custom",
    "name": "repo_acknowledge_finding",
    "description": (
        "Mark a Trivy finding as intentional by appending its rule ID to `.trivyignore` "
        "in the scenario directory on an existing fix branch. Use this when the finding "
        "is correct per the scanner but the rule is too strict for the use case "
        "(e.g. outbound HTTPS to 0.0.0.0/0 is required for any host that calls public "
        "APIs). Do NOT use this to hide real misconfigurations — the operator reviews "
        "every acknowledgment in the PR diff. Auto-approved."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "branch_name": {
                "type": "string",
                "description": "Existing fix branch (as returned by repo_create_branch_and_commit)",
            },
            "scenario_dir": {
                "type": "string",
                "description": (
                    "Scenario directory the .trivyignore lives in, e.g. 'open-ssh'. "
                    "Trivy scans each scenario dir independently so the ignore file must "
                    "be inside it."
                ),
            },
            "rule_id": {
                "type": "string",
                "description": "Trivy rule ID exactly as returned in the findings array, e.g. 'AVD-AWS-0104'",
            },
            "justification": {
                "type": "string",
                "description": (
                    "One-line reason this finding is intentional. Written as a comment "
                    "above the rule ID in .trivyignore so reviewers see the rationale."
                ),
            },
        },
        "required": ["branch_name", "scenario_dir", "rule_id", "justification"],
    },
}

ALL_TOOL_SCHEMAS = [
    REPO_CREATE_BRANCH_AND_COMMIT,
    REPO_UPDATE_BRANCH,
    REPO_OPEN_PULL_REQUEST,
    CI_GET_LATEST_STATUS,
    REPO_ACKNOWLEDGE_FINDING,
]

# Tools that require explicit human approval before execution.
APPROVAL_REQUIRED_TOOLS = {"repo_open_pull_request"}


# ---------------------------------------------------------------------------
# Executor Protocol
# ---------------------------------------------------------------------------

class ToolExecutor(Protocol):
    async def execute(self, name: str, inputs: dict) -> dict: ...


def _file_paths(files_changed: Any) -> list[str]:
    """Pull paths out of the files_changed input, tolerating older list-of-strings shape."""
    out: list[str] = []
    for item in files_changed or []:
        if isinstance(item, dict):
            path = item.get("path")
            if path:
                out.append(path)
        elif isinstance(item, str):
            out.append(item)
    return out


def _parse_sarif_zip(zip_bytes: bytes, scenario_dir: str) -> list[dict]:
    """Extract one finding dict per SARIF result inside the artifact zip.

    `scenario_dir` is parsed from the artifact name; SARIF location URIs are
    relative to the scan-ref (the scenario dir), so we prepend it to get a
    repo-relative path the agent can act on.
    """
    findings: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for entry in zf.namelist():
            if not entry.endswith(".sarif"):
                continue
            with zf.open(entry) as f:
                data = json.load(f)
            for run in data.get("runs", []):
                for result in run.get("results", []):
                    locations = result.get("locations") or [{}]
                    phys = locations[0].get("physicalLocation", {})
                    file_uri = phys.get("artifactLocation", {}).get("uri", "")
                    full_path = f"{scenario_dir}/{file_uri}" if file_uri else scenario_dir
                    findings.append({
                        "rule_id": result.get("ruleId", ""),
                        "severity": result.get("level", "warning"),
                        "scenario_dir": scenario_dir,
                        "file": full_path,
                        "line": phys.get("region", {}).get("startLine"),
                        "message": result.get("message", {}).get("text", ""),
                    })
    return findings


# ---------------------------------------------------------------------------
# Mock executor
# ---------------------------------------------------------------------------

class MockToolExecutor:
    """Returns realistic-looking results without doing real GitHub work."""

    REPO_URL = "https://github.com/asellers3rd/infraguard-agent"

    async def execute(self, name: str, inputs: dict) -> dict:
        if name == "repo_create_branch_and_commit":
            return {
                "branch": inputs.get("branch_name", "fix/auto-remediate"),
                "commit_sha": secrets.token_hex(4),
                "files_changed": _file_paths(inputs.get("files_changed")),
                "commit_url": f"{self.REPO_URL}/commit/{secrets.token_hex(4)}",
            }
        if name == "repo_update_branch":
            return {
                "branch": inputs.get("branch_name", "fix/auto-remediate"),
                "commit_sha": secrets.token_hex(4),
                "files_changed": _file_paths(inputs.get("files_changed")),
                "commit_url": f"{self.REPO_URL}/commit/{secrets.token_hex(4)}",
                "iteration": True,
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
                "findings": [],
            }
        if name == "repo_acknowledge_finding":
            return {
                "branch": inputs.get("branch_name", "fix/auto-remediate"),
                "rule_id": inputs.get("rule_id", "AVD-UNKNOWN"),
                "scenario_dir": inputs.get("scenario_dir", ""),
                "trivyignore_path": f"{inputs.get('scenario_dir', '')}/.trivyignore",
                "acknowledged": True,
            }
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Real GitHub executor
# ---------------------------------------------------------------------------

class GithubToolExecutor:
    """Calls the GitHub REST API to create real branches, commits, and PRs.

    Auth: fine-grained PAT scoped to a single lab repo with contents:write and
    pull-requests:write. Token comes from `GITHUB_TOKEN` env / .env.
    """

    BASE_URL = "https://api.github.com"

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        default_branch: str = "main",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.BASE_URL}/repos/{self.owner}/{self.repo}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "infraguard-agent",
                },
                timeout=20.0,
                transport=self._transport,
            )
        return self._client

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    async def execute(self, name: str, inputs: dict) -> dict:
        if name == "repo_create_branch_and_commit":
            return await self._create_branch_and_commit(inputs)
        if name == "repo_update_branch":
            return await self._update_branch(inputs)
        if name == "repo_open_pull_request":
            return await self._open_pull_request(inputs)
        if name == "ci_get_latest_status":
            return await self._get_ci_status(inputs)
        if name == "repo_acknowledge_finding":
            return await self._acknowledge_finding(inputs)
        raise ValueError(f"Unknown tool: {name}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Tool implementations -------------------------------------------------

    async def _create_branch_and_commit(self, inputs: dict) -> dict:
        client = self._http()
        requested_branch = inputs["branch_name"]
        commit_message = inputs["commit_message"]
        files = inputs.get("files_changed") or []

        # Resolve default branch tip
        resp = await client.get(f"/git/refs/heads/{self.default_branch}")
        resp.raise_for_status()
        base_sha = resp.json()["object"]["sha"]

        # Suffix the branch name so repeat runs of the same scenario don't collide.
        suffix = secrets.token_hex(3)
        branch_name = f"{requested_branch}-{suffix}"

        resp = await client.post(
            "/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
        resp.raise_for_status()

        last_commit_sha = base_sha
        committed_paths: list[str] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            content = entry.get("content")
            if not path or content is None:
                continue

            # Need the file's existing blob SHA on the branch to overwrite it.
            existing = await client.get(f"/contents/{path}", params={"ref": branch_name})
            put_body: dict[str, Any] = {
                "message": commit_message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch_name,
            }
            if existing.status_code == 200:
                put_body["sha"] = existing.json()["sha"]
            elif existing.status_code != 404:
                existing.raise_for_status()

            resp = await client.put(f"/contents/{path}", json=put_body)
            resp.raise_for_status()
            last_commit_sha = resp.json()["commit"]["sha"]
            committed_paths.append(path)

        return {
            "branch": branch_name,
            "commit_sha": last_commit_sha[:8],
            "files_changed": committed_paths,
            "commit_url": f"{self.repo_url}/commit/{last_commit_sha}",
        }

    async def _update_branch(self, inputs: dict) -> dict:
        """Push follow-up commits to an existing branch without creating a new ref.

        Confirms the branch exists, then PUTs each file's new content (passing the
        existing blob sha when the file is already present, so GitHub treats it as
        an update rather than a create).
        """
        client = self._http()
        branch_name = inputs["branch_name"]
        commit_message = inputs["commit_message"]
        files = inputs.get("files_changed") or []

        # Confirm the branch exists — surfaces a clean error if the agent passed a
        # branch name that was never created. raise_for_status() will 404 here.
        resp = await client.get(f"/git/refs/heads/{branch_name}")
        resp.raise_for_status()

        last_commit_sha = resp.json()["object"]["sha"]
        committed_paths: list[str] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            content = entry.get("content")
            if not path or content is None:
                continue

            existing = await client.get(f"/contents/{path}", params={"ref": branch_name})
            put_body: dict[str, Any] = {
                "message": commit_message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch_name,
            }
            if existing.status_code == 200:
                put_body["sha"] = existing.json()["sha"]
            elif existing.status_code != 404:
                existing.raise_for_status()

            resp = await client.put(f"/contents/{path}", json=put_body)
            resp.raise_for_status()
            last_commit_sha = resp.json()["commit"]["sha"]
            committed_paths.append(path)

        return {
            "branch": branch_name,
            "commit_sha": last_commit_sha[:8],
            "files_changed": committed_paths,
            "commit_url": f"{self.repo_url}/commit/{last_commit_sha}",
            "iteration": True,
        }

    async def _open_pull_request(self, inputs: dict) -> dict:
        client = self._http()
        resp = await client.post(
            "/pulls",
            json={
                "title": inputs["title"],
                "body": inputs.get("body", ""),
                "head": inputs["branch_name"],
                "base": self.default_branch,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "pr_number": data["number"],
            "pr_url": data["html_url"],
            "status": data["state"],
            "title": data["title"],
            "risk_level": inputs.get("risk_level", "medium"),
        }

    async def _get_ci_status(self, inputs: dict) -> dict:
        client = self._http()
        pr_number = inputs["pr_number"]

        resp = await client.get(f"/pulls/{pr_number}")
        resp.raise_for_status()
        head_sha = resp.json()["head"]["sha"]

        resp = await client.get(f"/commits/{head_sha}/check-runs")
        resp.raise_for_status()
        runs = resp.json().get("check_runs", [])

        if not runs:
            agg_status = "queued"
        elif any(r.get("conclusion") == "failure" for r in runs):
            agg_status = "failed"
        elif all(r.get("status") == "completed" for r in runs) and all(
            r.get("conclusion") == "success" for r in runs
        ):
            agg_status = "passed"
        else:
            agg_status = "running"

        checks = [
            {
                "name": r.get("name", "check"),
                "conclusion": r.get("conclusion") or r.get("status") or "queued",
            }
            for r in runs
        ]

        findings: list[dict] = []
        if agg_status == "failed":
            findings = await self._fetch_trivy_findings(head_sha)

        return {
            "status": agg_status,
            "duration_s": 0,
            "plan_summary": "See CI logs on the PR" if runs else "CI not yet started",
            "checks": checks,
            "findings": findings,
        }

    async def _fetch_trivy_findings(self, head_sha: str) -> list[dict]:
        """Best-effort fetch of structured Trivy findings for the head commit.

        Looks for workflow-run artifacts named `trivy-sarif-*` (one per scenario,
        uploaded by the lab repo's trivy.yml). Returns [] on any failure — diagnosis
        is helpful but not required, and we'd rather degrade to the legacy
        "CI failed, agent guesses" behavior than break the whole tool call.
        """
        client = self._http()
        try:
            resp = await client.get(
                "/actions/runs",
                params={"head_sha": head_sha, "per_page": 30},
            )
            resp.raise_for_status()
            workflow_runs = resp.json().get("workflow_runs", [])
            trivy_runs = [
                r for r in workflow_runs
                if r.get("name") == "trivy" and r.get("status") == "completed"
            ]
            if not trivy_runs:
                return []

            all_findings: list[dict] = []
            for run in trivy_runs:
                run_id = run["id"]
                arts_resp = await client.get(f"/actions/runs/{run_id}/artifacts")
                if arts_resp.status_code != 200:
                    continue
                for art in arts_resp.json().get("artifacts", []):
                    name = art.get("name", "")
                    if not name.startswith("trivy-sarif-"):
                        continue
                    scenario_dir = name[len("trivy-sarif-"):]
                    art_id = art["id"]
                    dl_resp = await client.get(
                        f"/actions/artifacts/{art_id}/zip",
                        follow_redirects=True,
                    )
                    if dl_resp.status_code != 200:
                        continue
                    all_findings.extend(
                        _parse_sarif_zip(dl_resp.content, scenario_dir)
                    )
            return all_findings
        except (httpx.HTTPError, ValueError, KeyError, zipfile.BadZipFile):
            logger.exception("Failed to fetch trivy findings; degrading gracefully")
            return []

    async def _acknowledge_finding(self, inputs: dict) -> dict:
        client = self._http()
        branch_name = inputs["branch_name"]
        scenario_dir = inputs["scenario_dir"]
        rule_id = inputs["rule_id"]
        justification = inputs["justification"]

        resp = await client.get(f"/git/refs/heads/{branch_name}")
        resp.raise_for_status()

        path = f"{scenario_dir}/.trivyignore"

        existing = await client.get(f"/contents/{path}", params={"ref": branch_name})
        if existing.status_code == 200:
            existing_data = existing.json()
            existing_sha: str | None = existing_data["sha"]
            existing_content = base64.b64decode(existing_data["content"]).decode("utf-8")
        elif existing.status_code == 404:
            existing_sha = None
            existing_content = ""
        else:
            existing.raise_for_status()
            existing_sha = None
            existing_content = ""

        if any(line.strip() == rule_id for line in existing_content.splitlines()):
            return {
                "branch": branch_name,
                "rule_id": rule_id,
                "scenario_dir": scenario_dir,
                "trivyignore_path": path,
                "acknowledged": True,
                "already_present": True,
            }

        prefix = existing_content
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        new_content = f"{prefix}# {justification}\n{rule_id}\n"

        put_body: dict[str, Any] = {
            "message": f"chore(security): acknowledge {rule_id} in {scenario_dir}",
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if existing_sha:
            put_body["sha"] = existing_sha

        resp = await client.put(f"/contents/{path}", json=put_body)
        resp.raise_for_status()
        commit_sha = resp.json()["commit"]["sha"]

        return {
            "branch": branch_name,
            "rule_id": rule_id,
            "scenario_dir": scenario_dir,
            "trivyignore_path": path,
            "commit_sha": commit_sha[:8],
            "commit_url": f"{self.repo_url}/commit/{commit_sha}",
            "acknowledged": True,
        }


# ---------------------------------------------------------------------------
# Factory — used by both the HTTP route layer and the CLI so they pick the
# same executor based on env config.
# ---------------------------------------------------------------------------


def build_executor_from_settings() -> ToolExecutor:
    """Return GithubToolExecutor if GITHUB_TOKEN is set, else MockToolExecutor."""
    from .config import settings

    if settings.github_configured:
        logger.info(
            "Using GithubToolExecutor against %s/%s",
            settings.github_owner,
            settings.github_repo,
        )
        return GithubToolExecutor(
            token=settings.github_token,
            owner=settings.github_owner,
            repo=settings.github_repo,
            default_branch=settings.github_default_branch,
        )
    logger.info("Using MockToolExecutor (GITHUB_TOKEN not set)")
    return MockToolExecutor()
