"""FastAPI endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import settings
from .runner import Runner, make_run_id
from .scenarios import get_scenario, list_scenarios, scenario_to_dict
from .sse import sse_response
from .store import store
from .tools import build_executor_from_settings


router = APIRouter()


class StartRunRequest(BaseModel):
    scenario_id: str


class StartRunResponse(BaseModel):
    run_id: str
    session_id: str


class HealthResponse(BaseModel):
    status: str
    anthropic_configured: bool
    github_configured: bool
    executor: str
    model: str


_runner: Runner | None = None


def get_runner() -> Runner:
    """Return the singleton Runner so approval gates persist across requests."""
    global _runner
    if not settings.anthropic_configured:
        raise HTTPException(
            status_code=503,
            detail="Anthropic API key not configured on the backend",
        )
    if _runner is None:
        from anthropic import Anthropic

        client = Anthropic(api_key=settings.anthropic_api_key)
        _runner = Runner(client, store, executor=build_executor_from_settings())
    return _runner


def reset_runner() -> None:
    """Used by tests to force re-construction."""
    global _runner
    _runner = None


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        anthropic_configured=settings.anthropic_configured,
        github_configured=settings.github_configured,
        executor="github" if settings.github_configured else "mock",
        model=settings.infraguard_model,
    )


@router.get("/scenarios")
async def get_scenarios() -> list[dict]:
    return [scenario_to_dict(s) for s in list_scenarios()]


@router.post("/runs", response_model=StartRunResponse)
async def start_run(req: StartRunRequest) -> StartRunResponse:
    scenario = get_scenario(req.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"Unknown scenario: {req.scenario_id}")

    runner = get_runner()
    run_id = make_run_id()
    await store.create_run(run_id, scenario.id, scenario.label)
    session_id = await runner.start_run(run_id, scenario)
    return StartRunResponse(run_id=run_id, session_id=session_id)


@router.get("/runs")
async def get_runs() -> list[dict]:
    return [run.to_dict() for run in store.list_runs()]


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run.to_dict()


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return sse_response(store, run_id)


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: str) -> dict:
    runner = get_runner()
    ok = await runner.approve(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="No pending approval for this run")
    return {"status": "approved"}


@router.post("/runs/{run_id}/reject")
async def reject_run(run_id: str) -> dict:
    runner = get_runner()
    ok = await runner.reject(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="No pending approval for this run")
    return {"status": "rejected"}
