"""Standalone CLI for end-to-end testing without the frontend.

Usage:
    python scripts/cli.py <scenario_id>

Available scenarios: open-ssh, missing-tags, public-s3, idle-compute

The CLI streams events to stdout as the agent works. When the agent reaches the
human-approval gate, the CLI prompts you to approve or reject. Approve to see
the simulated PR + CI + deploy events; reject to terminate.

Requires:
    ANTHROPIC_API_KEY set in environment or backend/.env
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make the package importable when running this script directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

from infraguard.config import settings  # noqa: E402
from infraguard.runner import Runner, make_run_id  # noqa: E402
from infraguard.scenarios import get_scenario, list_scenarios  # noqa: E402
from infraguard.store import event_to_dict, store  # noqa: E402
from infraguard.tools import build_executor_from_settings  # noqa: E402


# ANSI color codes for prettier event output
COLORS = {
    "signal_received": "\033[33m",  # yellow
    "session_started": "\033[36m",  # cyan
    "agent_message": "\033[37m",    # light gray
    "tool_call": "\033[35m",        # magenta
    "requires_action": "\033[31m\033[1m",  # bold red
    "approval_granted": "\033[32m", # green
    "pr_opened": "\033[34m",        # blue
    "ci_running": "\033[33m",       # yellow
    "ci_passed": "\033[32m",        # green
    "deployed": "\033[32m\033[1m",  # bold green
    "error": "\033[31m",            # red
}
RESET = "\033[0m"


def fmt_event(ev: dict) -> str:
    color = COLORS.get(ev["type"], "")
    ts = ev["timestamp"][11:19]  # HH:MM:SS
    return f"{color}[{ts}] {ev['type']:18s} {ev['message']}{RESET}"


async def prompt_approval(tr: dict) -> str:
    """Prompt the operator to approve or reject. Runs in a thread to avoid blocking the loop."""
    print()
    print("\033[33m" + "=" * 70 + RESET)
    print(f"\033[33m\033[1m  ACTION REQUIRES APPROVAL{RESET}")
    print(f"  Tool:        \033[37m{tr['toolName']}{RESET}")
    print(f"  Risk Level:  \033[31m{tr['riskLevel']}{RESET}")
    print(f"  Target Repo: \033[37m{tr['targetRepo']}{RESET}")
    print(f"  Summary:     \033[37m{tr['summary']}{RESET}")
    print("\033[33m" + "=" * 70 + RESET)

    def _get_input() -> str:
        return input("\nApprove? [y/N]: ").strip().lower()

    decision = await asyncio.to_thread(_get_input)
    return "approve" if decision in ("y", "yes") else "reject"


async def run_scenario(scenario_id: str) -> int:
    if not settings.anthropic_configured:
        print("\033[31mError: ANTHROPIC_API_KEY is not set.\033[0m")
        print("Set it in your shell or copy backend/.env.example to backend/.env")
        return 1

    scenario = get_scenario(scenario_id)
    if scenario is None:
        print(f"\033[31mError: unknown scenario '{scenario_id}'\033[0m")
        print("Available scenarios:")
        for s in list_scenarios():
            print(f"  - {s.id}: {s.label}")
        return 1

    print(f"\033[1m▶ InfraGuard CLI — running scenario: {scenario.label}\033[0m")
    print(f"  Model: {settings.infraguard_model}")
    print(f"  Severity: {scenario.severity}")
    print()

    client = Anthropic(api_key=settings.anthropic_api_key)
    executor = build_executor_from_settings()
    print(f"  Executor: {'github' if settings.github_configured else 'mock'}")
    if settings.github_configured:
        print(f"  Lab repo: {settings.github_owner}/{settings.github_repo}")
    runner = Runner(client, store, executor=executor)
    run_id = make_run_id()

    await store.create_run(run_id, scenario.id, scenario.label)
    await runner.start_run(run_id, scenario)

    queue = store.get_queue(run_id)
    if queue is None:
        print("\033[31mError: queue missing for run\033[0m")
        return 1

    # Stream events to stdout. Prompt at requires_action.
    while True:
        event = await queue.get()
        if event is None:
            break  # End-of-stream sentinel
        ev_dict = event_to_dict(event)
        print(fmt_event(ev_dict))

        if ev_dict["type"] == "requires_action" and ev_dict.get("toolRequest"):
            decision = await prompt_approval(ev_dict["toolRequest"])
            if decision == "approve":
                await runner.approve(run_id)
            else:
                await runner.reject(run_id)

    run = store.get_run(run_id)
    print()
    print(f"\033[1mFinal status: {run.status if run else 'unknown'}\033[0m")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/cli.py <scenario_id>")
        print()
        print("Available scenarios:")
        for s in list_scenarios():
            print(f"  - {s.id}: {s.label} ({s.severity})")
        return 1

    return asyncio.run(run_scenario(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
