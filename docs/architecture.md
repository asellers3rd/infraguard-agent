# InfraGuard Agent — Architecture

## System Overview

InfraGuard Agent is an AI-assisted infrastructure remediation system. It uses Claude Managed Agents to analyze IaC repositories, identify security and compliance violations, and propose fixes through an approval-gated workflow.

## Trust Boundaries

```
┌──────────────────────────────────────────────────────────────┐
│  YOUR INFRASTRUCTURE (Backend + CI/CD)                       │
│                                                              │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐  │
│  │ Backend API  │   │ Git Repo     │   │ CI/CD Pipeline   │  │
│  │ (tool exec)  │──>│ (PRs)        │──>│ (plan/validate)  │  │
│  │              │   │              │   │                  │  │
│  │ Credentials  │   │              │   │ Cloud Account    │  │
│  │ stored here  │   │              │   │ access here      │  │
│  └──────┬───────┘   └──────────────┘   └──────────────────┘  │
│         │                                                    │
└─────────┼────────────────────────────────────────────────────┘
          │ SSE events + custom tool calls
          │
┌─────────┼────────────────────────────────────────────────────┐
│  ANTHROPIC (Managed Agent Runtime)                           │
│         │                                                    │
│  ┌──────┴───────┐                                            │
│  │ Agent Session │  Isolated container                       │
│  │              │  - Limited networking (allowed_hosts)      │
│  │  bash, read, │  - No credentials in sandbox              │
│  │  write, grep │  - Web tools disabled                     │
│  │              │  - Files mounted read-only                 │
│  └──────────────┘                                            │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Event Flow

1. **Signal Received** — Operational alert triggers the workflow
2. **Session Created** — Backend creates a Managed Agent session with:
   - Agent config (model, system prompt, custom tools)
   - Environment (limited networking, packages)
   - Mounted files (IaC repo zip, runbook skills)
3. **Analysis Running** — Agent inspects Terraform files using built-in tools
4. **Tool Call Requested** — Agent emits `agent.custom_tool_use` (e.g., `repo_create_branch_and_commit`)
5. **Awaiting Approval** — Session goes idle with `stop_reason: requires_action`; UI shows approval gate
6. **Tool Executed** — Backend executes the tool (creates branch, opens PR) and returns `user.custom_tool_result`
7. **PR Opened** — Change proposal linked to the repo
8. **CI Validation** — Terraform plan, policy checks, cost estimation
9. **Deploy Outcome** — Merged, blocked, or rejected

## Custom Tools

| Tool | Purpose | Risk Level |
|------|---------|------------|
| `repo_create_branch_and_commit` | Create a fix branch with proposed changes | Medium |
| `repo_open_pull_request` | Open a PR with the fix | High |
| `ci_get_latest_status` | Check CI pipeline results | Low |

## Security Controls

- **Least privilege networking**: Container only reaches `allowed_hosts`
- **Credential isolation**: Secrets in vault, not in agent sandbox
- **Tool gating**: Destructive tools require human approval
- **Audit trail**: All events logged with timestamps and run IDs
- **CI enforcement**: No direct infrastructure changes — only code proposals
