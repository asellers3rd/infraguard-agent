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

When invoked, you are given a Terraform repository as a zip file at /workspace/repo.zip.
Your job:

1. Unzip the file with bash: `cd /workspace && unzip -o repo.zip`
2. Read the .tf files and identify the security or compliance issue
3. Use bash, grep, and read tools to investigate the violation thoroughly
4. Decide on a fix that is minimal, safe, and follows AWS best practices
5. Call `repo_create_branch_and_commit` with a descriptive branch name and the proposed
   file changes (this is auto-approved — feel free to commit your fix to a branch)
6. Call `repo_open_pull_request` with a clear title, body, and risk_level — this requires
   human approval, and the operator may approve or reject your proposal
7. After the PR is approved and opened, call `ci_get_latest_status` to check CI results
8. Briefly summarize what you did and what the operator should know

Be concise. Use bullet points. Reference specific file paths and line numbers when possible.
Do not modify the original repo.zip file (it is read-only). Do not attempt destructive
actions outside the custom tools provided.
"""


ENVIRONMENT_CONFIG = {
    "type": "cloud",
    "packages": {
        "apt": ["unzip"],
    },
    "networking": {
        "type": "limited",
        "allowed_hosts": [],  # No external network access needed for IaC analysis
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


def _find_or_create_agent(client: Anthropic) -> str:
    # Search existing agents by name
    for agent in client.beta.agents.list():
        if getattr(agent, "name", None) == AGENT_NAME:
            logger.info("Reusing existing agent: %s", agent.id)
            return agent.id

    logger.info("Creating new agent: %s", AGENT_NAME)
    agent = client.beta.agents.create(
        name=AGENT_NAME,
        model=settings.infraguard_model,
        system=SYSTEM_PROMPT,
        tools=[
            # Built-in toolset for bash, file read/write/edit, grep
            {
                "type": "agent_toolset_20260401",
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
            },
            *ALL_TOOL_SCHEMAS,
        ],
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
