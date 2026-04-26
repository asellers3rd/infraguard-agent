"""In-memory run + event state, asyncio-safe."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

RunStatus = Literal[
    "pending", "running", "awaiting_approval", "approved", "rejected", "completed", "failed"
]


@dataclass
class ToolRequest:
    tool_name: str
    risk_level: str
    target_repo: str
    summary: str
    parameters: dict
    event_id: str  # the agent.custom_tool_use event id we'll respond to


@dataclass
class RunEvent:
    id: str
    timestamp: str
    type: str
    message: str
    tool_request: ToolRequest | None = None


@dataclass
class Run:
    id: str
    scenario_id: str
    scenario_label: str
    session_id: str | None
    status: RunStatus
    started_at: str
    completed_at: str | None = None
    events: list[RunEvent] = field(default_factory=list)
    pending_tool_request: ToolRequest | None = None
    metrics: dict = field(default_factory=lambda: {
        "timeToFirstToken": 0,
        "timeToPR": 0,
        "estimatedCost": 0.0,
    })

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scenarioId": self.scenario_id,
            "scenarioLabel": self.scenario_label,
            "status": self.status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "metrics": self.metrics,
            "toolRequest": _tool_request_to_dict(self.pending_tool_request),
        }


def _tool_request_to_dict(tr: ToolRequest | None) -> dict | None:
    if tr is None:
        return None
    return {
        "toolName": tr.tool_name,
        "riskLevel": tr.risk_level,
        "targetRepo": tr.target_repo,
        "summary": tr.summary,
    }


def event_to_dict(ev: RunEvent) -> dict:
    return {
        "id": ev.id,
        "timestamp": ev.timestamp,
        "type": ev.type,
        "message": ev.message,
        "toolRequest": _tool_request_to_dict(ev.tool_request),
    }


class RunStore:
    """Async-safe in-memory store for runs and a per-run event queue."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._queues: dict[str, asyncio.Queue[RunEvent | None]] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, run_id: str, scenario_id: str, scenario_label: str) -> Run:
        async with self._lock:
            run = Run(
                id=run_id,
                scenario_id=scenario_id,
                scenario_label=scenario_label,
                session_id=None,
                status="pending",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            self._runs[run_id] = run
            self._queues[run_id] = asyncio.Queue()
            return run

    async def attach_session(self, run_id: str, session_id: str) -> None:
        async with self._lock:
            run = self._runs[run_id]
            run.session_id = session_id

    async def update_status(self, run_id: str, status: RunStatus) -> None:
        async with self._lock:
            run = self._runs[run_id]
            run.status = status
            if status in ("completed", "failed", "rejected"):
                run.completed_at = datetime.now(timezone.utc).isoformat()

    async def set_pending_tool_request(self, run_id: str, tr: ToolRequest | None) -> None:
        async with self._lock:
            self._runs[run_id].pending_tool_request = tr

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.events.append(event)
            queue = self._queues.get(run_id)
        if queue is not None:
            await queue.put(event)

    async def close_stream(self, run_id: str) -> None:
        """Signal end-of-stream for a run by pushing None onto its queue."""
        queue = self._queues.get(run_id)
        if queue is not None:
            await queue.put(None)

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def get_queue(self, run_id: str) -> asyncio.Queue[RunEvent | None] | None:
        return self._queues.get(run_id)

    def list_runs(self) -> list[Run]:
        return list(self._runs.values())


store = RunStore()
