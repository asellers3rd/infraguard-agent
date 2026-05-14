"""Get-or-create the Anthropic agent + environment, idempotent.

The agent and environment are defined declaratively here. On startup we
look for existing resources by name; if they exist we reuse them, otherwise
we create them. This means restarting the backend doesn't churn through
new agent/environment IDs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import Anthropic

from .config import settings
from .tools import ALL_TOOL_SCHEMAS

logger = logging.getLogger(__name__)


AGENT_NAME = "infraguard-iac-remediator"
ENVIRONMENT_NAME = "infraguard-terraform-env"

SYSTEM_PROMPT = """You are InfraGuard, an AI infrastructure remediation agent. You analyze
Terraform code for security and compliance violations and propose fixes.

When invoked, you are given a Terraform repository as a zip file at
`/mnt/session/uploads/repo.zip`. Your job:

1. Unzip the file into a writable working dir:
   `mkdir -p /workspace && unzip -o /mnt/session/uploads/repo.zip -d /workspace`
2. `cd /workspace` and read the .tf files to identify the security or compliance issue
3. Use bash, grep, and read tools to investigate the violation thoroughly
4. Decide on a fix that is minimal, safe, and follows AWS best practices
5. Write the fixed file(s) to disk (e.g. `/workspace/open-ssh/main.tf`),
   then call `repo_create_branch_and_commit`. For `files_changed`, pass an array of
   objects: `[{"path": "<repo-relative-path>", "content": "<full file content after fix>"}]`.
   Paths are relative to the lab repo root (the scenario directory name comes first,
   e.g. `open-ssh/main.tf`). The `content` must be the COMPLETE file body, not a diff.
   This call is auto-approved.
6. Call `repo_open_pull_request` with a clear title, body, and risk_level — this requires
   human approval, and the operator may approve or reject your proposal
7. After the PR is approved and opened, call `ci_get_latest_status` to check CI results.
   If CI reports failures (e.g. tfsec/Trivy flagged an additional finding, Infracost flagged
   a policy violation), analyze the failure, fix the additional file(s) on disk, and call
   `repo_update_branch` with the SAME branch name returned by `repo_create_branch_and_commit`.
   Do NOT call `repo_create_branch_and_commit` again — that creates a sibling branch and a
   duplicate PR. The existing PR will automatically pick up your follow-up commits. After
   pushing the iteration, call `ci_get_latest_status` again to confirm the fix landed.
8. Briefly summarize what you did and what the operator should know. If you iterated, note
   how many iterations it took and what CI feedback drove each one.

Be concise. Use bullet points. Reference specific file paths and line numbers when possible.
Do not modify the original repo.zip file (it is read-only). Do not attempt destructive
actions outside the custom tools provided.

[Tool schema version: v3-iterative-fix-loop]
"""


ENVIRONMENT_CONFIG = {
    "type": "cloud",
    "packages": {
        "apt": ["unzip"],
    },
    "networking": {
        "type": "limited",
        "allowed_hosts": [],  # No external network access needed for IaC analysis
        "allow_package_managers": True,  # Required so apt can install `unzip` at env build
    },
}


@dataclass
class AgentResources:
    agent_id: str
    environment_id: str
    model: str


def get_or_create_resources(client: Anthropic) -> AgentResources:
    """Look up or create the agent + environment by name. Idempotent."""
    agent_id = _find_or_create_agent(client)
    environment_id = _find_or_create_environment(client)
    return AgentResources(
        agent_id=agent_id,
        environment_id=environment_id,
        model=settings.infraguard_model,
    )


_AGENT_TOOLS = [
    # Built-in toolset for bash, file read/write/edit, grep
    {
        "type": "agent_toolset_20260401",
        "default_config": {
            "enabled": True,
            "permission_policy": {"type": "always_allow"},
        },
    },
    *ALL_TOOL_SCHEMAS,
]


def _find_or_create_agent(client: Anthropic) -> str:
    # Search existing agents by name. The system prompt has a [Tool schema version: ...]
    # marker — bumping that marker whenever ALL_TOOL_SCHEMAS changes guarantees the
    # drift check below also re-syncs tools, so we don't need a separate tool-equality
    # comparison against API-returned objects.
    for agent in client.beta.agents.list():
        if getattr(agent, "name", None) != AGENT_NAME:
            continue
        if getattr(agent, "system", None) != SYSTEM_PROMPT:
            logger.info("Updating drifted system prompt + tools on agent: %s", agent.id)
            client.beta.agents.update(
                agent.id,
                version=agent.version,
                system=SYSTEM_PROMPT,
                tools=_AGENT_TOOLS,
            )
        else:
            logger.info("Reusing existing agent: %s", agent.id)
        return agent.id

    logger.info("Creating new agent: %s", AGENT_NAME)
    agent = client.beta.agents.create(
        name=AGENT_NAME,
        model=settings.infraguard_model,
        system=SYSTEM_PROMPT,
        tools=_AGENT_TOOLS,
    )
    return agent.id


def _find_or_create_environment(client: Anthropic) -> str:
    for env in client.beta.environments.list():
        if getattr(env, "name", None) == ENVIRONMENT_NAME:
            logger.info("Reusing existing environment: %s", env.id)
            return env.id

    logger.info("Creating new environment: %s", ENVIRONMENT_NAME)
    env = client.beta.environments.create(
        name=ENVIRONMENT_NAME,
        config=ENVIRONMENT_CONFIG,
    )
    return env.id


_cached_resources: AgentResources | None = None


def get_resources(client: Anthropic) -> AgentResources:
    """Cache the lookup so we don't list agents/envs on every run."""
    global _cached_resources
    if _cached_resources is None:
        _cached_resources = get_or_create_resources(client)
    return _cached_resources


def reset_cache() -> None:
    """Used by tests to force re-resolution."""
    global _cached_resources
    _cached_resources = None
