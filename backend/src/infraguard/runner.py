"""Session lifecycle: open SSE stream, kick off the agent, handle requires_action, dispatch approve/reject.

The runner translates Anthropic events into the dashboard's existing event types so the frontend
components don't need to change. See the translation table in the plan.

Concurrency model: each run's `_stream_loop` runs in a worker thread (via asyncio.to_thread).
Worker threads schedule store mutations and event emits onto the main event loop using
asyncio.run_coroutine_threadsafe(), so all asyncio primitives (Event, Queue, Lock) live on
a single loop.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic

from .agent import get_resources
from .scenarios import Scenario, build_scenario_zip
from .store import RunEvent, RunStore, ToolRequest
from .tools import APPROVAL_REQUIRED_TOOLS, MockToolExecutor, ToolExecutor

logger = logging.getLogger(__name__)


@dataclass
class ApprovalGate:
    event: asyncio.Event
    decision: str | None = None  # "approve" | "reject"
    tool_request: ToolRequest | None = None


class Runner:
    """Drives one or more concurrent runs against the Managed Agents API."""

    def __init__(self, client: Anthropic, store: RunStore, executor: ToolExecutor | None = None) -> None:
        self.client = client
        self.store = store
        self.executor = executor or MockToolExecutor()
        self._gates: dict[str, ApprovalGate] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    # -- Public API used by routes / CLI ------------------------------------

    async def start_run(self, run_id: str, scenario: Scenario) -> str:
        """Create a session, mount the scenario zip, kick off the agent.

        Returns the Anthropic session_id. Spawns a background task that drives
        the event loop to completion. The caller subscribes to the run's
        event queue via store.get_queue() to receive updates.
        """
        self._get_loop()  # capture the loop reference
        await self.store.update_status(run_id, "running")
        await self._emit(run_id, "signal_received", f"Triggered scenario: {scenario.label}")

        session_id = await asyncio.to_thread(self._create_session, scenario)
        await self.store.attach_session(run_id, session_id)
        await self._emit(run_id, "session_started", f"Managed Agent session {session_id} created")

        gate = ApprovalGate(event=asyncio.Event())
        self._gates[run_id] = gate

        asyncio.create_task(self._drive_session(run_id, session_id, scenario, gate))
        return session_id

    async def approve(self, run_id: str) -> bool:
        gate = self._gates.get(run_id)
        if gate is None or gate.event.is_set():
            return False
        gate.decision = "approve"
        gate.event.set()
        return True

    async def reject(self, run_id: str) -> bool:
        gate = self._gates.get(run_id)
        if gate is None or gate.event.is_set():
            return False
        gate.decision = "reject"
        gate.event.set()
        return True

    # -- Session creation (sync, called via to_thread) ----------------------

    def _create_session(self, scenario: Scenario) -> str:
        resources = get_resources(self.client)

        zip_bytes = build_scenario_zip(scenario)
        zip_buf = io.BytesIO(zip_bytes)
        zip_buf.name = f"{scenario.id}.zip"
        zip_file = self.client.beta.files.upload(file=zip_buf)

        session = self.client.beta.sessions.create(
            agent={"type": "agent", "id": resources.agent_id},
            environment_id=resources.environment_id,
            resources=[
                {
                    "type": "file",
                    "file_id": zip_file.id,
                    # API resolves this relative to /mnt/session/uploads/, so the file
                    # lands at /mnt/session/uploads/repo.zip — referenced by the system prompt.
                    "mount_path": "repo.zip",
                }
            ],
            title=f"InfraGuard: {scenario.label}",
        )
        return session.id

    # -- Event loop ----------------------------------------------------------

    async def _drive_session(
        self,
        run_id: str,
        session_id: str,
        scenario: Scenario,
        gate: ApprovalGate,
    ) -> None:
        """Stream Anthropic events, handle requires_action, dispatch tool results."""
        try:
            await asyncio.to_thread(self._stream_loop, run_id, session_id, scenario, gate)
            run = self.store.get_run(run_id)
            if run and run.status not in ("completed", "failed", "rejected"):
                await self.store.update_status(run_id, "completed")
        except Exception as exc:
            logger.exception("Session driver failed for run %s", run_id)
            await self._emit(run_id, "error", f"Session error: {exc}")
            await self.store.update_status(run_id, "failed")
        finally:
            await self.store.close_stream(run_id)
            self._gates.pop(run_id, None)

    def _stream_loop(
        self,
        run_id: str,
        session_id: str,
        scenario: Scenario,
        gate: ApprovalGate,
    ) -> None:
        """Synchronous SSE event loop — runs in a worker thread."""
        events_by_id: dict[str, Any] = {}

        with self.client.beta.sessions.events.stream(session_id) as stream:
            # Open the stream BEFORE sending the kickoff message
            self.client.beta.sessions.events.send(
                session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"A Terraform repository has been mounted at "
                                    f"/mnt/session/uploads/repo.zip. "
                                    f"Scenario: {scenario.label}. {scenario.description}. "
                                    f"Analyze the code, identify the issue, and propose a fix following "
                                    f"the workflow in your system prompt."
                                ),
                            }
                        ],
                    }
                ],
            )

            for ev in stream:
                events_by_id[ev.id] = ev
                self._handle_event(run_id, ev, events_by_id, session_id, gate)

                if ev.type == "session.status_idle":
                    stop_reason = getattr(ev, "stop_reason", None)
                    if stop_reason and getattr(stop_reason, "type", None) == "end_turn":
                        return
                if ev.type in ("session.status_terminated", "session.error"):
                    return

    def _handle_event(
        self,
        run_id: str,
        ev: Any,
        events_by_id: dict[str, Any],
        session_id: str,
        gate: ApprovalGate,
    ) -> None:
        """Translate one Anthropic event into one (or zero) dashboard events.

        Synchronous — schedules emits onto the main asyncio loop via run_coroutine_threadsafe.
        """
        ev_type = ev.type

        if ev_type == "agent.message":
            text = self._extract_text(ev)
            if text:
                self._emit_threadsafe(run_id, "agent_message", text)

        elif ev_type == "agent.tool_use":
            tool_name = getattr(ev, "name", "tool")
            self._emit_threadsafe(run_id, "tool_call", f"Built-in tool: {tool_name}")

        elif ev_type == "agent.custom_tool_use":
            tool_name = getattr(ev, "name", "")
            self._emit_threadsafe(run_id, "tool_call", f"Custom tool requested: {tool_name}")

        elif ev_type == "session.status_idle":
            stop_reason = getattr(ev, "stop_reason", None)
            if stop_reason and getattr(stop_reason, "type", None) == "requires_action":
                event_ids = list(getattr(stop_reason, "event_ids", []))
                self._handle_requires_action(run_id, event_ids, events_by_id, session_id, gate)

        elif ev_type == "session.error":
            self._emit_threadsafe(run_id, "error", "Session error from Anthropic API")

    # -- requires_action loop ------------------------------------------------

    def _handle_requires_action(
        self,
        run_id: str,
        event_ids: list[str],
        events_by_id: dict[str, Any],
        session_id: str,
        gate: ApprovalGate,
    ) -> None:
        for event_id in event_ids:
            call = events_by_id.get(event_id)
            if call is None or call.type != "agent.custom_tool_use":
                continue

            tool_name = getattr(call, "name", "")
            tool_input = getattr(call, "input", {}) or {}

            if tool_name in APPROVAL_REQUIRED_TOOLS:
                self._await_approval(run_id, event_id, tool_name, tool_input, session_id, gate)
            else:
                self._execute_and_send(run_id, event_id, tool_name, tool_input, session_id)

    def _execute_and_send(
        self,
        run_id: str,
        event_id: str,
        tool_name: str,
        tool_input: dict,
        session_id: str,
    ) -> None:
        """Run the tool executor (on the main loop) and post the result back to the session."""
        loop = self._get_loop()
        future = asyncio.run_coroutine_threadsafe(
            self.executor.execute(tool_name, tool_input), loop
        )
        result = future.result()
        self._emit_derived_events_from_result(run_id, tool_name, result)
        self.client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": event_id,
                    "content": [{"type": "text", "text": json.dumps(result)}],
                }
            ],
        )

    def _await_approval(
        self,
        run_id: str,
        event_id: str,
        tool_name: str,
        tool_input: dict,
        session_id: str,
        gate: ApprovalGate,
    ) -> None:
        risk_level = tool_input.get("risk_level", "high")
        summary = tool_input.get("body") or tool_input.get("title") or "Pending operator decision"
        target_repo = tool_input.get("repo") or "asellers3rd/infraguard-agent"

        tr = ToolRequest(
            tool_name=tool_name,
            risk_level=risk_level,
            target_repo=target_repo,
            summary=summary,
            parameters=tool_input,
            event_id=event_id,
        )
        loop = self._get_loop()

        # Set pending state on the main loop
        asyncio.run_coroutine_threadsafe(
            self.store.set_pending_tool_request(run_id, tr), loop
        ).result()
        asyncio.run_coroutine_threadsafe(
            self.store.update_status(run_id, "awaiting_approval"), loop
        ).result()
        gate.tool_request = tr

        self._emit_threadsafe(
            run_id,
            "requires_action",
            f"Awaiting operator approval: {tool_name}",
            tool_request=tr,
        )

        # Block this worker thread until the dashboard sends approve/reject.
        # The Event lives on the main loop; we await it from there.
        asyncio.run_coroutine_threadsafe(gate.event.wait(), loop).result()

        if gate.decision == "approve":
            asyncio.run_coroutine_threadsafe(
                self.store.update_status(run_id, "approved"), loop
            ).result()
            asyncio.run_coroutine_threadsafe(
                self.store.set_pending_tool_request(run_id, None), loop
            ).result()
            self._emit_threadsafe(run_id, "approval_granted", "Operator approved tool execution")
            self._execute_and_send(run_id, event_id, tool_name, tool_input, session_id)
        else:
            asyncio.run_coroutine_threadsafe(
                self.store.update_status(run_id, "rejected"), loop
            ).result()
            asyncio.run_coroutine_threadsafe(
                self.store.set_pending_tool_request(run_id, None), loop
            ).result()
            self._emit_threadsafe(run_id, "error", "Operator rejected tool execution. Run terminated.")
            self.client.beta.sessions.events.send(
                session_id,
                events=[{"type": "user.interrupt"}],
            )

    # -- Helpers -------------------------------------------------------------

    def _emit_derived_events_from_result(self, run_id: str, tool_name: str, result: dict) -> None:
        if tool_name == "repo_open_pull_request":
            pr_url = result.get("pr_url", "")
            pr_number = result.get("pr_number", "")
            self._emit_threadsafe(run_id, "pr_opened", f"PR #{pr_number} opened: {pr_url}")
        elif tool_name == "repo_update_branch":
            branch = result.get("branch", "?")
            paths = result.get("files_changed") or []
            self._emit_threadsafe(
                run_id,
                "iteration_pushed",
                f"Pushed follow-up commit to {branch} ({len(paths)} file(s))",
            )
        elif tool_name == "repo_acknowledge_finding":
            rule_id = result.get("rule_id", "?")
            scenario_dir = result.get("scenario_dir", "?")
            if result.get("already_present"):
                msg = f"{rule_id} already acknowledged in {scenario_dir}/.trivyignore"
            else:
                msg = f"Acknowledged {rule_id} in {scenario_dir}/.trivyignore"
            self._emit_threadsafe(run_id, "finding_acknowledged", msg)
        elif tool_name == "ci_get_latest_status":
            status = result.get("status", "unknown")
            duration = result.get("duration_s", "?")
            plan = result.get("plan_summary", "")
            findings = result.get("findings") or []
            self._emit_threadsafe(run_id, "ci_running", "CI started")
            if status == "passed":
                self._emit_threadsafe(run_id, "ci_passed", f"CI passed in {duration}s — {plan}")
                self._emit_threadsafe(run_id, "deployed", "PR ready to merge")
            elif status == "failed" and findings:
                rules = ", ".join(sorted({f.get("rule_id", "?") for f in findings})[:5])
                self._emit_threadsafe(
                    run_id,
                    "ci_findings",
                    f"CI failed with {len(findings)} finding(s): {rules}",
                )

    def _extract_text(self, ev: Any) -> str:
        content = getattr(ev, "content", None)
        if not content:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
                else:
                    text = getattr(item, "text", None)
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        return ""

    async def _emit(
        self,
        run_id: str,
        ev_type: str,
        message: str,
        *,
        tool_request: ToolRequest | None = None,
    ) -> None:
        await self.store.append_event(run_id, _build_event(ev_type, message, tool_request))

    def _emit_threadsafe(
        self,
        run_id: str,
        ev_type: str,
        message: str,
        *,
        tool_request: ToolRequest | None = None,
    ) -> None:
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._emit(run_id, ev_type, message, tool_request=tool_request),
            loop,
        ).result()


def _build_event(ev_type: str, message: str, tool_request: ToolRequest | None) -> RunEvent:
    return RunEvent(
        id=f"evt_{uuid.uuid4().hex[:12]}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        type=ev_type,
        message=message,
        tool_request=tool_request,
    )


def make_run_id() -> str:
    return f"run_{secrets.token_hex(6)}"
