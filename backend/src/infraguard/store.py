"""In-memory run + event state, asyncio-safe."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .drift import DriftFinding

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


class DriftStore:
    """Async-safe in-memory store for drift findings.

    Re-scans upsert by finding id (the id is deterministic from
    scenario+resource, see drift.make_finding_id). A finding seen in a previous
    scan but absent from the current scan is marked `resolved` rather than
    removed, so the dashboard can show "X was fixed at <ts>" history.
    """

    def __init__(self) -> None:
        self._findings: dict[str, DriftFinding] = {}
        self._lock = asyncio.Lock()
        self._last_scanned_at: str | None = None

    async def apply_scan(self, fresh: list[DriftFinding]) -> dict[str, int]:
        """Merge a fresh scan into the store. Returns counts for telemetry."""
        async with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self._last_scanned_at = now
            fresh_by_id = {f.id: f for f in fresh}
            new_count = 0
            updated_count = 0
            resolved_count = 0

            for fid, finding in fresh_by_id.items():
                existing = self._findings.get(fid)
                if existing is None:
                    self._findings[fid] = finding
                    new_count += 1
                else:
                    existing.last_seen_at = now
                    existing.evidence = finding.evidence
                    existing.description = finding.description
                    # If the user had marked it remediating and we still see it,
                    # leave the status alone — the agent may still be working.
                    # If it was resolved and reappeared, reopen.
                    if existing.status == "resolved":
                        existing.status = "open"
                        existing.run_id = None
                    updated_count += 1

            for fid, existing in self._findings.items():
                if fid not in fresh_by_id and existing.status != "resolved":
                    existing.status = "resolved"
                    resolved_count += 1

            return {
                "new": new_count,
                "updated": updated_count,
                "resolved": resolved_count,
                "total": len(self._findings),
            }

    async def mark_remediating(self, finding_id: str, run_id: str) -> DriftFinding | None:
        async with self._lock:
            finding = self._findings.get(finding_id)
            if finding is None:
                return None
            finding.status = "remediating"
            finding.run_id = run_id
            return finding

    def get(self, finding_id: str) -> DriftFinding | None:
        return self._findings.get(finding_id)

    def list_findings(self) -> list[DriftFinding]:
        # Sorted: open first, then remediating, then resolved; within each,
        # newest detection first so the dashboard surfaces the most recent.
        # Stable sort lets us do this in two passes.
        order = {"open": 0, "remediating": 1, "resolved": 2}
        by_recency = sorted(self._findings.values(), key=lambda f: f.detected_at, reverse=True)
        return sorted(by_recency, key=lambda f: order.get(f.status, 9))

    @property
    def last_scanned_at(self) -> str | None:
        return self._last_scanned_at


drift_store = DriftStore()
