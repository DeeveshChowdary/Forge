# Handoff

_Last updated: 2026-11-10._ Read with [ARCHITECTURE.md](ARCHITECTURE.md) and
[TEST_RESULTS.md](TEST_RESULTS.md).

## State

All eight milestones from the original brief are implemented and verified:

| # | Milestone | Status |
|---|---|---|
| 1 | Foundation & backend (SQLite schema, state machine, services) | done, tested |
| 2 | Agent adapters & model discovery (Claude Code, Codex, mock) | done, tested against fakes and the real CLIs |
| 3 | Persistent workflow engine (leases, recovery, bounded loops, approvals, decomposition) | done, tested |
| 4 | Heuristic router (4 modes, overrides, persisted reasons) | done, tested |
| 5 | Web UI (all required pages, dark/light, responsive, live SSE) | done, component + e2e tested, browser-checked |
| 6 | Multi-agent integration (mock suite + real Claude→Codex handoff) | done |
| 7 | Project import & memory | done, tested |
| 8 | Read-only R4 integration (Claude + Codex proposals compared) | done; see TEST_RESULTS.md |

Real records live in `~/.forge/forge.db` (projects `handoff-demo`, `R4`); view them with
`forge serve` → http://127.0.0.1:8765.

## Known gaps / not built

* **Paid-API adapters**: none. The router, billing labels and spend limits are in place;
  an Anthropic/OpenAI API adapter would implement `ProviderAdapter` and report
  `billing="api"` and `cost_kind="billed"`.
* **GitHub automation**: detection only. No push/PR creation (would need an approval kind
  `publish` wired to `gh`, which is not installed here).
* **Learned routing**: interface only (`RoutingPolicy`); heuristic policy in use.
* **MCP for Codex**: MCP config is passed to Claude Code only.
* **Browser/research integration**: a permission toggle for agents' native web tools; no
  Forge-side browser automation.
* **Session continuation**: adapters support `resume_session_id`, but the engine uses fresh
  sessions with structured handoffs by default (deliberate); no UI toggle yet.
* **Auth**: the API has no authentication; it is protected only by binding to localhost
  plus host/CSRF guards. Do not expose it.
* The Claude Code CLI here is 2.1.72; `claude-opus-5-5` needs ≥ 2.1.280. Updating the CLI is
  the user's decision (`claude update`).

## Operational notes

* macOS privacy protection blocks processes started by some app launchers from reading
  `~/Desktop` (Python hangs in `getcwd`). Start Forge from a terminal (or use the
  `~/research/forge` symlink from a terminal) rather than from such a launcher.
* If Claude probes fail with 401 "OAuth access token has expired", re-login with `claude`
  → `/login`, then **Agent control → Verify unverified models** (or `forge doctor --probe`).
* `forge run` executes in the foreground when no `forge serve` engine is running. Two
  engines never run at once (engine lock); a second process only enqueues.

* **Disk space**: on 2026-11-10 the data volume was 98% full (4.1 GiB free), which made
  the whole machine slow (macOS cache purging at high CPU). Forge itself uses ~180 MB
  (`.venv`, `web/node_modules`, `~/.forge`). Free space before long agent runs.
* A read-only Codex self-review of Forge's core (`forge-self` project, task
  `t_cb9f201194f5`, `gpt-6.1-sol` high) stalled with no output for 15+ minutes under that
  load and was cancelled via `forge cancel`. Re-run it when the machine is healthy:
  `forge run forge-self "<review prompt>" --workflow analyze --read-only --route analyze=codex:gpt-6.1-sol:high`.

## Exact next steps (suggested)

1. Use Forge on a real project: `forge project add <path> --test-command "<cmd>"`, then
   create a task in the UI.
2. Review the R4 proposals (R4 project → task "R4: propose next research milestone" →
   Proposals tab) and decide whether to adopt one; Forge did not change R4.
3. Optional hardening: API token for the local server; Codex MCP pass-through; GitHub PR
   approval flow.

## M9 — external coordination (merged into main and deployed 2026-11-11)

Developed in the worktree `~/research/forge-m9` so the running instance (started from
the main checkout) was never touched. See docs/EXTERNAL_COORDINATION.md,
docs/GITHUB_BRIDGE.md, docs/REMOTE_ACCESS.md.

**Observed during M9 (not caused by it):** the Independent Research task
`t_fa827e885e2d` (R4 M1 deep prior-art review) FAILED at 22:56 with "turn limit 40
exceeded" in the `experiment` phase on both attempts; partial edits were saved as a diff
artifact. Raise the project's `max_turns` (e.g. 150) and retry.

### Safe activation (after no task is executing)

1. Confirm nothing is running: `forge status` shows no PLANNING/EXECUTING/TESTING/
   REVIEWING tasks (or wait for them to finish).
2. Back up the database: `sqlite3 ~/.forge/forge.db ".backup ~/.forge/forge-pre-m9.db"`.
3. Stop the server (Ctrl-C in its terminal; running tasks would be requeued, not lost).
4. Merge the branch: `cd ~/research/forge && git merge --ff-only dev/m9-external-coordination`
   (or review the diff first), then `pnpm --dir web build`.
5. Start from your own terminal: `~/research/forge/.venv/bin/forge serve`. The new tables
   are created on open; the bridge stays **disabled**.
6. Configure GitHub access (fine-grained token, Issues read/write on the research repo,
   via `gh auth login` or the keychain) and run `forge bridge check`.
7. Create the `forge-task` label, then on the Coordination page review authorized authors
   and projects and click **Enable bridge**.
8. Live verification: open one test issue (your approval needed to post in the research
   repo) and confirm discovery, authorization, status comment and final summary.
9. Rollback: stop the server, `git checkout` the previous commit; the extra tables are
   ignored by the old code (or restore the backup).

## M10 (merged into main and deployed 2026-11-11)

See docs/M10_RCA.md for the root cause of t_fa827e885e2d and the recovery plan (awaiting
approval; staged in ~/.forge/recovery/t_fa827e885e2d/). Deployed following the
M9 procedure above.
