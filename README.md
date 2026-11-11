# Forge

A local-first, multi-model AI engineering harness. Describe a goal in the web UI; Forge
plans it, routes each phase to a suitable model (Claude Code, Codex, …), runs agents in
isolated git worktrees, runs your tests, has a different model review the work, loops
on confirmed issues a bounded number of times, and asks you before anything touches your
working tree. Everything is persisted so it survives restarts.

* **Any project**: create, import a folder, or clone a repo. Projects are isolated
  (config, worktrees, sessions, memory, history, permissions, limits).
* **Multi-provider**: Claude Code CLI and Codex CLI adapters with runtime model discovery
  and probe-based verification. No model is assumed available.
* **Explainable routing**: balanced / max quality / economy / manual, per-phase
  overrides, persisted reasons for every decision.
* **Workflows**: Plan→Implement→Test→Review, Debug→Reproduce→Fix→Regression, Research→
  Hypothesis→Experiment→Analyze, Design→Build→Validate→Deliver, independent proposals
  (read-only, side-by-side comparison), single read-only analysis.
* **Safety**: worktree isolation, approvals for merges/spending, read-only mode, untrusted
  repository instructions, no API keys passed to agents (subscription CLIs only by default).

See [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md),
[DEVELOPMENT.md](DEVELOPMENT.md), [TEST_RESULTS.md](TEST_RESULTS.md), [HANDOFF.md](HANDOFF.md).

## Requirements

* macOS or Linux, Python ≥ 3.12, git ≥ 2.20, Node ≥ 20 with pnpm (to build the UI)
* At least one agent CLI, already logged in:
  * Claude Code (`claude`) — log in with `claude` then `/login`
  * Codex (`codex`) — on PATH, or the binary bundled with the ChatGPT/Codex VS Code
    extension (auto-detected)

## Start

```bash
cd ~/research/forge            # symlink to this repository
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
pnpm --dir web install && pnpm --dir web build
.venv/bin/forge doctor --probe # detect providers and verify each model with a tiny prompt
.venv/bin/forge serve          # open http://127.0.0.1:8765
```

`forge doctor --probe` sends one short prompt per enabled model using your existing
subscriptions. Without it, models stay *unverified* and are not routed to (you can also
verify from **Agent control → Verify unverified models**).

## Your first project

1. **Projects → Add project.** Choose *Import folder* (path to an existing repo),
   *Clone Git URL*, or *Create new*. Click **Inspect** to get a suggested test command
   (e.g. `{python} -m pytest -q`; `{python}` is Forge's interpreter, which has pytest).
   Folders without git history are imported read-only.
2. Optional: in the project's **Overview**, trust `AGENTS.md`/`CLAUDE.md` if agents should
   follow them; in **Settings**, adjust permissions and limits.
3. **New task.** Describe the goal, pick a workflow, keep *Automatic* routing or choose
   models per phase, click **Preview routing** to see which models will run and why, then
   **Start task**.
4. Watch the task page: phase timeline, live agent log, runs with exact model ids and
   token usage, structured handoffs (plan, review, test report), the cumulative diff, and
   routing reasons.
5. When it finishes, approve the merge (or **Request revision** to send the review back to
   the implementer, or reject to keep the branch only).

## CLI

All commands use the same database and engine as the UI (`FORGE_HOME`, default
`~/.forge`).

| Command | Purpose |
|---|---|
| `forge doctor [--probe]` | environment check, provider/auth status, model verification |
| `forge serve [--port 8765] [--no-engine]` | API + web UI + execution engine |
| `forge worker [TASK] [--until-idle]` | execution engine without the web server |
| `forge project add PATH\|URL [--create] [--read-only] [--test-command CMD] [--name N]` | register a project |
| `forge project list [--json]` | list projects |
| `forge plan PROJECT [--workflow W] [--mode M] [--complexity C] [--route phase=provider:model[:effort]]` | dry-run: phases and routing decisions with reasons |
| `forge run PROJECT "goal" [--workflow W] [--mode M] [--route …] [--review independent] [--approval never_merge] [--accept CRITERION] [--decompose] [--read-only] [--detach]` | submit a task; runs in the foreground unless a server engine is running |
| `forge status [TASK]` | list tasks or show runs of one task |
| `forge pause\|resume\|cancel\|retry TASK` | task controls (work from any process) |
| `forge revise TASK [--note TEXT]` | send the latest review + note back to the fix phase |
| `forge approve\|reject APPROVAL [--note TEXT]` | decide a pending approval |
| `forge logs TASK [-f] [-n N]` | event log, optionally following |

Examples:

```bash
forge project add ~/code/my-app --test-command "{python} -m pytest -q"
forge plan my-app --complexity high --mode max_quality
forge run my-app "Add rate limiting to the /login endpoint with tests" \
  --route plan=claude_code:claude-sonnet-5-5 --route implement=codex:gpt-6.1-sol:medium
```

## External coordination (M9)

External assistants such as ChatGPT can submit tasks, monitor them and send feedback
**through GitHub issues** in an allowlisted private repository, without anyone relaying
messages between agents. Local tools can use the token-scoped
`/api/v1/coordination/*` API or the stdio MCP server (`forge mcp`). Configure everything
on the **Coordination** page. See [docs/EXTERNAL_COORDINATION.md](docs/EXTERNAL_COORDINATION.md),
[docs/GITHUB_BRIDGE.md](docs/GITHUB_BRIDGE.md) and [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md).

| Command | Purpose |
|---|---|
| `forge bridge status\|check\|enable\|disable\|sync\|run` | GitHub bridge |
| `forge token create NAME --project P [--scope tasks:write] [--scope feedback:write]` / `list` / `revoke ID` | coordination API tokens |
| `forge mcp` | stdio MCP server (needs `FORGE_COORDINATION_TOKEN`) |

## Verified on this machine (2026-11-10)

* Claude Code CLI 2.1.72 (Claude Pro, subscription) — verified: `sonnet`→`claude-sonnet-4-6`,
  `opus`→`claude-opus-4-6`, `haiku`→`claude-haiku-4-5-20251001`, `claude-sonnet-5-5`.
  `claude-opus-5-5` is rejected by this CLI version (needs ≥ 2.1.280).
* Codex CLI 0.164.0-alpha (bundled with the VS Code extension, ChatGPT login) — verified:
  `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6.1-terra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`,
  `gpt-5.6-terra`, `gpt-5.6-luna`.

Details and real run records: [TEST_RESULTS.md](TEST_RESULTS.md).

## Status

Working: everything listed above, with tests. Not yet built: paid-API adapters, GitHub
push/PR automation, learned routing, multi-user auth. See [HANDOFF.md](HANDOFF.md).

## Collaboration policies and literature research (M10)

* `--collaboration auto|cross_model|independent_research|implement_review|manual` (CLI) or the
  *Collaboration policy* field in New task. Policies are enforced by the engine; if a required
  provider is unavailable the task is refused or blocked — never silently run on one model.
* `literature_review` workflow: Literature Review → Independent Analysis (Claude and Codex
  separately) → Novelty Assessment → Cross-Review → GO/PIVOT/REJECT/INCONCLUSIVE Decision.
  Reports are written to `forge-reports/<task>/` in the task worktree and committed.
* `forge plan PROJECT --workflow W --collaboration P --objective "…"` shows the provider/model
  per phase plus preflight warnings before anything runs.
* Limits are per phase; a limit hit preserves partial work as a checkpoint and continues once;
  `forge retry TASK --max-turns N --continue` for deliberate retries. See docs/M10_RCA.md.
