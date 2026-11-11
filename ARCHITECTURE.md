# Forge architecture

Forge is a local-first orchestration harness. One Python process (`forge serve`) hosts
the HTTP API, the web UI and the execution engine. The CLI uses the same services and
the same SQLite database, so every interface sees the same state.

```
            ┌──────────── web UI (React/Vite, web/) ─────────────┐
            │  REST + Server-Sent Events (/api/events/stream)    │
            └───────────────────────┬────────────────────────────┘
 forge CLI ──────────┐              │
                     ▼              ▼
              ┌────────────── forge/ (Python) ───────────────┐
              │ api.py ── services.py ── memory.py           │
              │              │                               │
              │ engine.py ── router.py ── workflows.py       │
              │    │              │                          │
              │    │        providers/registry.py            │
              │    │   claude_code.py  codex.py  mock.py     │
              │ gitops.py (worktrees)   integrations.py      │
              │ events.py ── db.py (SQLite, WAL) ~/.forge    │
              └──────────────────────────────────────────────┘
```

## Modules

| Module | Responsibility |
|---|---|
| `db.py` | Schema, thread-local connections, `BEGIN IMMEDIATE` transactions (serialises writers across processes). |
| `domain.py` | `TaskState`, the transition table, `ProjectConfig` (permissions, limits), `GlobalSettings`. |
| `services.py` | Projects (create/import/clone, overlap checks, instruction-file trust), tasks (transitions, deps, pause/resume/cancel/retry), artifacts, approvals. |
| `memory.py` | Per-project memory with FTS5 retrieval (relevance + recency + importance + task affinity), char-budgeted. |
| `workflows.py` | Workflow templates: phases → (state, role, schema, writes?). Bounded `fix` loop. |
| `schemas.py` | Strict JSON schemas for structured handoffs (plan, handoff, review, report, proposal). |
| `prompts.py` | Builds prompts from the task contract, trusted instructions, retrieved memory and handoff artifacts. |
| `router.py` | `RoutingPolicy` interface + `HeuristicPolicy`; decisions persisted with reasons and all candidates. |
| `engine.py` | Scheduler, leases, phase execution, test runner, fan-out/compare, approvals, decomposition, recovery. |
| `providers/` | Adapter interface (`base.py`), subprocess runner (`proc.py`), Claude Code, Codex, mock, registry. |
| `gitops.py` | Worktrees per task, commits inside worktrees, diffs, guarded merge. |
| `integrations.py` | Per-project integrations: git, local files, testing, GitHub, browser, Zotero (read-only), MCP. |
| `api.py` | FastAPI app, host/CSRF guards, SSE stream, SPA hosting. |
| `cli.py` | `forge doctor|serve|project|plan|run|status|pause|resume|cancel|retry|logs|approve|reject`. |

## Task state machine

```
CREATED → READY → PLANNING → EXECUTING → TESTING → REVIEWING → COMPLETED
                     │  ▲         ▲  │        │         │
                     │  └── fix ──┴──┴────────┴─────────┘   (bounded by max_review_cycles)
  any active → PAUSED | CANCELLED | FAILED | BLOCKED | AWAITING_APPROVAL
```

The phase state is the workflow phase's state (e.g. `research` and `hypothesis` are both
`PLANNING`). `TRANSITIONS` in `domain.py` is the single source of truth; illegal
transitions raise `InvalidTransition`. `FAILED → READY` exists only for an explicit retry.

## Execution model

1. **Engine lock.** One row in `engine_lock` (owner, pid, heartbeat). Only the owner
   executes. A second process (e.g. `forge run` while `forge serve` runs) only enqueues.
2. **Scheduling.** Each tick: propagate pause/cancel flags written by other processes,
   check decomposed parents, then claim `READY` tasks whose dependencies are `COMPLETED`,
   respecting per-project and global concurrency. Claims are an atomic
   `UPDATE … WHERE state='READY' AND lease free`, so no task is driven twice.
3. **Workspace.** Writing workflows get a dedicated `git worktree` on `forge/<task>` from
   the project's HEAD (or configured base branch) under `~/.forge/worktrees/<project>/`.
   Read-only tasks run directly in the project root with read-only sandboxes.
4. **Phase run.** Route → create run row → build prompt → adapter executes → persist the
   result, usage, artifact; for writing phases commit the worktree and store the diff.
   The phase pointer advances only after these writes, so recovery re-runs only the
   unfinished phase.
5. **Review loop.** A `request_changes` verdict with blocker/major issues, or failing
   tests, moves to `fix` (implementer's route unless overridden), then test and review
   again — at most `max_review_cycles` times. Exhausted cycles → `AWAITING_APPROVAL`
   (`review_unresolved`) or `FAILED` for failing tests.
6. **Finish.** No changes → `COMPLETED`. Changes → merge approval (unless the policy is
   `never_merge`, or auto-merge is explicitly configured). Merge refuses a dirty tree.

### Recovery

On acquiring the lock, `Engine.recover()`:
* kills orphaned agent processes recorded on `running` runs (after checking the pid's
  command still looks like an agent CLI — guards against pid reuse) and marks the runs
  `interrupted`;
* saves partial uncommitted worktree changes as a `diff` artifact, then resets the
  worktree to the last Forge commit;
* requeues interrupted tasks in the same phase (`READY`), honouring pending pause/cancel
  requests; repeated interruptions beyond `max_retries + 2` fail the task.

Graceful shutdown (`Ctrl-C` / server stop) cancels running agents and requeues tasks
without counting an attempt.

## Providers

`ProviderAdapter` (providers/base.py): `detect()`, `discover_models()`, `probe(model)`,
`execute(request, on_event, cancel, on_start)`, `supported_efforts()`.

| | Claude Code (`claude -p`) | Codex (`codex exec`) |
|---|---|---|
| Discovery | none in CLI → configured entries, probed | `codex debug models` catalog |
| Exact model id | from `modelUsage` keys in the result | the slug accepted by `exec` |
| Structured output | `--json-schema` → `structured_output` | `--output-schema` + `-o` last message |
| Effort | `--effort low|medium|high|max` | `-c model_reasoning_effort=…` (per-model levels) |
| Read-only | `--tools Read,Grep,Glob` + disallow write/shell | `-s read-only` (OS sandbox) |
| Write | `acceptEdits`, Edit/Write + scoped `Bash(prefix:*)` | `-s workspace-write` in the worktree |
| Isolation | `--setting-sources user`, `--strict-mcp-config`, `--settings` with empty `attribution` | `--ignore-user-config` (auth still from CODEX_HOME) |
| Resume | `--resume <session>` | `exec resume <thread> -` |
| Usage | tokens + `total_cost_usd` (subscription estimate) | tokens only |

Both run through `proc.run_streaming`: own process group, wall-clock timeout, cooperative
cancellation, step/turn limit enforced by counting streamed agent steps, raw log per run,
and an allowlisted environment (no API keys, no host-session variables).

Models are routable only when `verified` (probe or successful run). An auth failure
marks the *provider* `expired` and leaves models `unverified`; a model-specific
rejection marks the model `failed` with the provider's message.

## Router

`HeuristicPolicy.select(ctx)`:
* **Required tier** from complexity (low/medium/high → fast/standard/strong), +1 for high
  criticality; `max_quality` forces strong for planner/reviewer/analyst roles; `economy`
  starts one tier lower and escalates one tier per failed attempt.
* **Exclusions** (recorded with reasons): provider not installed, auth expired/missing,
  model failed or unverified, directly billed API without both opt-ins, recent rate-limit.
* **Score** = tier fit (overshoot penalised to avoid unnecessary escalation) + quality
  (capped at the requirement, except in `max_quality`) − cost index + history (smoothed
  success × confidence n/(n+5)) − latency + user/project preference − same-provider
  penalty for reviewers.
* **Independent review**: reviewers avoid the provider(s) that wrote the code; with
  `review_depth=independent` or `max_quality` they are excluded when an alternative exists.
* **Effort** from complexity and mode, clamped to the model's supported levels, never
  auto-selecting `max`/`ultra`.
* **Manual** mode/overrides validate the model, provider, billing and effort; no silent
  fallback.

Tier and cost index per model are editable data (Agent control center); the defaults are
name-based guesses labelled as such. A learned policy can implement the same `select()`
and `record_outcome()`; `RouteContext` carries the features and `routing_decisions`
stores outcomes.

## Memory

`memory` rows: kind (requirement, decision, hypothesis, milestone, failure, test_result,
attempt, problem, user_decision, note, research), title, content, source task/run,
importance, shared, pinned. Every entry needs a source. Retrieval runs FTS5 over the
current project (plus `shared` rows of other projects only when `share_memory` is on),
adds pinned/same-task/recent rows, scores and trims to a character budget. The engine
writes plans, review problems, failures, test results, milestones and approval decisions.

## Data layout

```
~/.forge/                 (FORGE_HOME)
  forge.db                SQLite (WAL)
  worktrees/<project>/<task>/
  logs/<task>/<run>.log   raw provider stream per run (+ schema/last-message files)
  projects/               projects created from scratch / cloned
```

## Extending

* **Provider**: subclass `ProviderAdapter`, register it in `ProviderRegistry._build`.
  Set billing honestly in `detect()`; the router excludes `api` billing unless allowed.
* **Workflow**: add a `Workflow` to `WORKFLOWS` (phases, review phase, fix phase).
* **Integration**: subclass `Integration`, add to `integrations.REGISTRY`.
* **Router**: implement `RoutingPolicy` and pass it to `Router(forge, policy)`.

## External coordination layer (M9)

```
 GitHub issues/comments ──poll── coordination/bridge.py ─┐
 HTTP /api/v1/coordination (bearer) ── coordination/api.py ├─► coordination/service.py ─► engine / services / bus
 stdio MCP ── coordination/mcp.py ────────────────────────┘        (scopes, project limits, sanitize.py)
```

* `service.py` — principal-scoped facade: reads (projects, tasks, events, artifacts,
  agents) built only from persisted rows; writes call `submit_task` and
  `Engine.request_revision`. External tasks are created `CREATED → AWAITING_APPROVAL`
  with an `external_task` approval unless authorization is disabled; approving moves
  them to `READY`, rejecting cancels them. Agents get external feedback as an
  `external_feedback` handoff artifact and a provenance note in their prompt.
* `tokens.py` — SHA-256-hashed bearer tokens with scopes `read`, `tasks:write`,
  `feedback:write` and a project list; in-process token-bucket rate limit.
* `bridge.py` — polling worker (lock in `worker_locks`), allowlisted repos/authors,
  `forge-task` JSON schema, idempotent `gh_links` / `gh_comments`, outbox `gh_outbox`
  with unique keys, status comment edited in place, exponential backoff.
* `github.py` — REST client (httpx), ETag caching, rate-limit tracking, typed errors;
  token from env, `gh auth token`, or keychain — never stored.
* Schema changes are additive (`CREATE TABLE IF NOT EXISTS`); the only new task
  transition is `CREATED → AWAITING_APPROVAL`.
* The bridge worker starts with `forge serve` and does nothing until enabled.
