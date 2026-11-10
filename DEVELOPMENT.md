# Developing Forge

## Layout

```
forge/            Python backend (FastAPI, engine, providers, CLI)
  providers/      adapter interface + Claude Code, Codex, mock adapters
tests/            pytest suites (unit, adapter, router, engine integration, API/CLI)
web/              React + TypeScript + Vite + Tailwind UI
  src/pages/      one file per page
  src/components/ shared UI
  e2e/            Playwright tests (+ serve.sh, an isolated seeded server)
```

## Setup

```bash
uv venv --python 3.14 .venv            # any Python ≥ 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
pnpm --dir web install
pnpm --dir web build                   # FastAPI serves web/dist at /
```

## Running

```bash
.venv/bin/forge serve                  # http://127.0.0.1:8765 (UI + API + engine)
```

UI development with hot reload (proxies `/api` to the running server):

```bash
pnpm --dir web dev                     # http://127.0.0.1:5173
```

Use a throwaway data directory while developing so your real `~/.forge` stays clean:

```bash
FORGE_HOME=$PWD/.forge-local/dev .venv/bin/forge serve --port 8766
```

## Tests

```bash
.venv/bin/pytest -q                    # backend: 53 tests, ~30s, no real models used
pnpm --dir web test                    # component tests (Vitest + Testing Library)
pnpm --dir web exec playwright test    # e2e in headless Google Chrome (channel: chrome)
FORGE_LIVE_TESTS=1 .venv/bin/pytest -q -m live   # optional: probes the real CLIs
```

The e2e suite starts `web/e2e/serve.sh`, which creates a fresh `FORGE_HOME` and a
disposable git repo under `$TMPDIR/forge-e2e`, enables only the mock provider and serves
on port 8799. Build the UI first (`pnpm --dir web build`).

Backend suites:

| File | Covers |
|---|---|
| `test_core.py` | state machine, project registration/overlap, read-only import, instruction trust, task controls, deps, memory scoping |
| `test_providers.py` | Claude/Codex adapters against fake CLIs emitting real formats: parsing, structured output, usage, auth/rate-limit classification, turn limits, timeouts, cancellation killing the process tree, argv safety, env scrubbing, discovery |
| `test_router.py` | tier selection per mode, economy escalation, effort capping, independent review, exclusions, paid-API gating, manual validation, history, persistence |
| `test_engine.py` | mock-agent integration: happy path + merge approval, review fix loop, bounded loops, failing tests → fix, retries, cancel, pause/resume, crash recovery without duplicate execution, worktree isolation + dirty-tree protection, runtime/spend/concurrency limits, read-only proposals, decomposition, per-phase approvals, engine lock |
| `test_api_cli.py` | guards (host, CSRF), full HTTP task flow, mock gating, providers/preview, integrations/memory, CLI end-to-end |

## Conventions

* All state changes go through `services.TaskService.transition` (validated, emits
  `task.state`). Do not update `tasks.state` directly.
* Every external process: argv list, `proc.clean_env()`, timeout, process group.
* New provider: implement `ProviderAdapter`; report billing honestly; never assume a model
  is available — return configured entries and let probes verify them.
* UI: all mock-provider output must render `<MockBadge />`.
* Keep memory entries sourced (`source` or `source_task_id`).

## Mock scenarios

With the mock provider enabled (Settings → Providers), tasks accept a `mock` object
(API field or the "Mock scenario" box in New task):

```json
{"files": {"path": "content"}, "fix_files": {"path": "content"},
 "reviews": ["request_changes", "approve"], "fail": {"implementer": 1},
 "sleep_s": 2, "subtasks": [{"title": "A", "objective": "…", "depends_on": []}]}
```
