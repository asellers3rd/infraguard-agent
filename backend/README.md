# InfraGuard Backend

Python backend that runs Claude Managed Agents against the Terraform lab scenarios in this repo. Provides a FastAPI HTTP layer and a CLI for end-to-end testing.

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

You'll need an Anthropic API key with Managed Agents access. Get one at https://console.anthropic.com/settings/keys.

## CLI demo

Run a single scenario end-to-end against the real Anthropic API:

```bash
python scripts/cli.py open-ssh
```

Available scenarios: `open-ssh`, `missing-tags`, `public-s3`, `idle-compute`.

The CLI streams events as the agent works, pauses when it requests human approval (the hero moment), and prompts you to approve or reject. Approve, and you'll see the simulated PR + CI + deploy events.

## HTTP API

```bash
uvicorn infraguard.main:app --reload --port 8000
```

Endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Backend health + Anthropic config status |
| `GET` | `/scenarios` | List available scenarios |
| `POST` | `/runs` | Start a new run, returns `{run_id, session_id}` |
| `GET` | `/runs/{run_id}/events` | SSE stream of run events |
| `POST` | `/runs/{run_id}/approve` | Approve a pending tool call |
| `POST` | `/runs/{run_id}/reject` | Reject and terminate a run |
| `GET` | `/runs` | List recent runs |

## Tests

```bash
pytest
```

## Cost

Each scenario run costs roughly $0.05–$0.10:

- Anthropic platform fee: $0.08/session-hour (a typical run is 60–90 seconds → $0.002)
- Token cost: depends on the model and how chatty the agent is
- Recommended: set a monthly budget cap in your Anthropic console

## Architecture

The backend is split into focused modules:

- `agent.py` — get-or-create the agent + environment definitions (idempotent)
- `scenarios.py` — build zips from `../terraform-lab/<scenario>/` and serve them via the Anthropic Files API
- `tools.py` — custom tool schemas (`repo_create_branch_and_commit`, `repo_open_pull_request`, `ci_get_latest_status`) with mock executors
- `runner.py` — session lifecycle: open SSE stream, kick off the agent, handle `requires_action`, dispatch approve/reject
- `routes.py` + `main.py` + `sse.py` — FastAPI HTTP layer
- `store.py` — in-memory run state (asyncio-safe)

The custom tools are mock implementations — they return realistic-looking values without doing real GitHub work. This keeps demos reliable and free of external dependencies. Real GitHub integration is on the roadmap.

## Safety model

- Agent runs in Anthropic's sandboxed container (no access to local network, secrets, or filesystem)
- Custom tools run on the backend, outside the agent sandbox
- The privileged `repo_open_pull_request` tool requires explicit human approval — the session pauses until the operator approves or rejects
- All events are logged with timestamps and run IDs for audit
