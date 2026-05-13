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

### Optional: real GitHub PRs

By default the agent uses a mock executor — it returns realistic-looking results but
doesn't touch GitHub. To have the agent open real pull requests, point it at a
disposable lab repo:

1. Create a public GitHub repo (e.g. `asellers3rd/infraguard-lab`) and push the
   contents of `../terraform-lab/` to its `main` branch. This repo is a sacrificial
   target — every demo run opens a PR against it.
2. Generate a fine-grained personal access token at
   https://github.com/settings/personal-access-tokens/new scoped to the lab repo with:
   - Contents: **Read and write**
   - Pull requests: **Read and write**
   - Metadata: **Read** (auto-included)
3. Add to `backend/.env`:
   ```
   GITHUB_TOKEN=github_pat_...
   GITHUB_OWNER=asellers3rd
   GITHUB_REPO=infraguard-lab
   GITHUB_DEFAULT_BRANCH=main
   ```

`GET /health` returns `executor: "github"` once configured. Drop the token to revert
to the mock executor — no other code changes needed.

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
- `tools.py` — custom tool schemas plus two `ToolExecutor` implementations: `MockToolExecutor` (default, no external calls) and `GithubToolExecutor` (real GitHub REST API; selected when `GITHUB_TOKEN` is set)
- `runner.py` — session lifecycle: open SSE stream, kick off the agent, handle `requires_action`, dispatch approve/reject
- `routes.py` + `main.py` + `sse.py` — FastAPI HTTP layer
- `store.py` — in-memory run state (asyncio-safe)

The mock executor returns realistic-looking values without external dependencies, keeping demos reliable. Setting `GITHUB_TOKEN` (see Setup above) swaps in the real `GithubToolExecutor` — same Protocol, no other code changes — and the agent creates actual branches, commits, and pull requests on the configured lab repo.

## Safety model

- Agent runs in Anthropic's sandboxed container (no access to local network, secrets, or filesystem)
- Custom tools run on the backend, outside the agent sandbox
- The privileged `repo_open_pull_request` tool requires explicit human approval — the session pauses until the operator approves or rejects
- All events are logged with timestamps and run IDs for audit
