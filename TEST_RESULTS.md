# Test results

Recorded 2026-11-10 on macOS 14 (Darwin 23.2.0, arm64), Python 3.14.0, Node 25.2.0,
git 2.39.3. Real-run records are in `~/.forge/forge.db` and visible in the UI.

## Automated suites

| Suite | Command | Result |
|---|---|---|
| Backend (unit, adapter, router, engine integration, API, CLI) | `.venv/bin/pytest -q` | **56 passed**, 1 skipped (opt-in live probe test) |
| Frontend components (Vitest + Testing Library) | `pnpm --dir web test` | **11 passed** |
| End-to-end (Playwright, headless Google Chrome) | `pnpm --dir web exec playwright test` | **3 passed** |
| Type check | `pnpm --dir web exec tsc --noEmit -p .` | clean |

Coverage of the requested categories:

| Category | Where |
|---|---|
| Unit tests | `tests/test_core.py` (state machine, projects, tasks, deps, memory) |
| Provider-adapter tests | `tests/test_providers.py` — fake CLIs emitting the real Claude stream-json / Codex JSONL formats |
| Mock-agent integration | `tests/test_engine.py` — real engine, real worktrees, real pytest runs, mock models |
| Task-state tests | `test_core.py::test_transition_table_basics`, `::test_task_lifecycle_and_controls`, engine pause/cancel/approval tests |
| Crash recovery | `test_engine.py::test_crash_recovery_no_duplicate_execution` (orphan process killed, partial edits saved + reset, completed phase not re-run) |
| Worktree isolation | `test_engine.py::test_worktree_isolation_and_dirty_tree_protection` |
| Router selection | `tests/test_router.py` (10 tests) |
| Budget enforcement | `::test_runtime_limit`, `::test_known_api_spend_limit_blocks`, `::test_concurrency_limit`, `::test_retries_exhausted`, bounded review loop |
| Frontend component tests | `web/src/components/ui.test.tsx` |
| End-to-end browser tests | `web/e2e/forge.spec.ts` (import project → task with routing preview → live log → runs → diff → approve merge; agent control; CSRF rejection) |

## Provider discovery and verification (real)

`forge doctor --probe` (one tiny read-only prompt per model):

| Provider | Version / auth | Model entry | Source | Status | Exact model id |
|---|---|---|---|---|---|
| Claude Code CLI | 2.1.72, claude.ai Pro (subscription) | `sonnet` | configured | verified | `claude-sonnet-4-6` |
| | | `opus` | configured | verified | `claude-opus-4-6` |
| | | `haiku` | configured | verified | `claude-haiku-4-5-20251001` |
| | | `claude-sonnet-5-5` | configured | verified | `claude-sonnet-5-5` |
| | | `claude-opus-5-5` | configured | **failed** | CLI: "Claude Code 2.1.72 does not support this model; version 2.1.280 or newer is required" |
| Codex CLI | 0.164.0-alpha.11.1 (VS Code extension bundle), ChatGPT login (subscription) | `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6.1-terra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` | discovered (`codex debug models`) | verified | same as slug |
| | | `gpt-reserve`, `codex-auto-review` | discovered, catalog-hidden | not probed (disabled) | — |

Notes:
* At the start of the session the stored Claude login was expired (every call: 401
  "OAuth access token has expired"). Forge reported `auth expired` and kept models
  unverified. The user re-logged in; probes then passed.
* Codex is not on PATH; Forge found the binary bundled with the VS Code extension.
* Reasoning efforts: Claude `low|medium|high|max` (CLI flag); Codex per model from the
  catalog (`low…ultra`; Forge never auto-selects `max`/`ultra`).

## Real Claude → Codex handoff (required test)

Project `handoff-demo` (`~/.forge/projects/handoff-demo`, disposable). Task
`t_0044233fdd18` "Claude→Codex handoff: duration parsing" — `parse_duration` /
`format_duration` with strict validation and a round-trip property. Manual routing.

| # | Phase | Agent / exact model | Effort | Result | Time | Tokens in / out | Cost |
|---|---|---|---|---|---|---|---|
| 1 | plan | Claude Code `claude-sonnet-5-5` | medium | structured plan | 23.0s | 13,175 / 2,920 | $0.102 subscription-equivalent (not billed) |
| 2 | implement | Codex `gpt-6.1-sol` | low | `durations.py` + tests | 58.2s | 94,144 / 2,317 | not reported |
| 3 | test | Forge test runner | — | **65 passed** | 0.8s | — | — |
| 4 | review | Claude Code `claude-sonnet-5-5` | high | approve; 1 minor (committed `__pycache__/*.pyc`), 1 nit (ambiguous error message) | 14.8s | 17,523 / 1,766 | $0.097 sub.-equiv. |
| — | user | `forge revise` with the two confirmed issues | — | fix phase queued (cycle 1) | — | — | — |
| 5 | fix | Codex `gpt-6.1-sol` | low | removed bytecode, added `.gitignore`, distinct messages | 22.8s | 60,582 / 717 | not reported |
| 6 | test | Forge test runner | — | **73 passed** | 0.8s | — | — |
| 7 | review | Claude Code `claude-sonnet-5-5` | high | approve (2 nits) | 11.4s | 10,876 / 1,231 | $0.066 sub.-equiv. |
| 8 | merge | user approval | — | merged into `main` at `9913be0` | — | — | — |

Observations, stated plainly:
* Claude's first review **approved** — it found only minor/nit issues, which do not trigger
  Forge's automatic fix loop (blocker/major only). Step 5 therefore ran through the
  user-initiated **Request revision** path (added during this test), not the automatic
  loop. The automatic loop is covered by the mock suite.
* The `.pyc` issue was a real Forge defect (it committed bytecode left by the agent's own
  test run). Fixed: newly added cache artefacts are no longer committed, and agents get
  `PYTHONDONTWRITEBYTECODE=1`.
* Claude reviewers stated they had read the diff and not run the tests themselves; Forge's
  own test runs are the test evidence.
* Every phase, routing decision (with reasons), artifact, test report, diff, event and
  approval is stored; the UI's task page showed all 7 runs with these exact models,
  efforts, durations and token counts.

Earlier preflight (all Codex, automatic routing, low complexity) — task `t_f14682101fe0`:
plan 16.7s → implement 26.8s → tests **5 passed** → review 3.6s, all `gpt-6-luna` effort
low, chosen by the balanced router as the cheapest adequate verified model. Changes left
on the branch (policy `never_merge`).

## R4 read-only integration (required test)

Project `R4` = `~/research/r4-computation-migration`, registered **read-only** (also its
git repository has no commits, which Forge would have required for write mode anyway).

* Forge read, through its file layer: `AGENTS.md` (5,825 B), `docs/CURRENT_STATE.md`
  (project status), `docs/RESEARCH_PROTOCOL.md`, `proposals/hypotheses.md`,
  `docs/TEST_MATRIX.md` (existing test results), `results/discovery-smoke.json`,
  `literature/` (7 entries), `literature/reviews/` (2 review summaries),
  `literature/notes/` (27 notes). Path traversal (`../../.ssh/id_rsa`) was rejected.
* `AGENTS.md` and `CLAUDE.md` were detected and left **untrusted** (not injected into
  prompts); agents read them as data.
* Task `t_840c379bffeb`, workflow `independent_proposals`, run in parallel:

| Proposer | Effort | Time | Tokens in / out | Proposed milestone |
|---|---|---|---|---|
| Claude Code `claude-sonnet-5-5` | high | 47.3s | 36,909 / 6,633 ($0.265 sub.-equiv.) | "M1: Source-grounded closest-prior-art extraction and snowballing, ending in a revised novelty matrix and a go/no-go note on the semantic contract" |
| Codex `gpt-6.1-sol` | medium | 67.6s | 140,189 / 2,151 | "M1: Source-grounded prior-art review and restricted migration semantics" |

* Both converge on finishing locator-backed extraction for R4-05–10, R4-14–17 and R4-23,
  snowballing, and a recorded research decision; Codex additionally drafts a restricted
  semantic contract and worked counterexamples inside the milestone, while Claude defers
  that to M2 and adds baseline checks and an explicit annotation/regeneration step.
* Forge's deterministic comparison (after improving it during this test): 4 paired steps
  (similarities 0.65 snowballing, 0.44 full-text extraction, 0.29 research decision, 0.28
  weaker pairing), 4 / 3 unpaired steps, 16 shared cited files. It measures wording overlap,
  not quality, and the UI says so.
* **No R4 changes:** SHA-256 of all 174 files (excluding `.venv`, `.git`, caches) identical
  before and after; `git status --porcelain` identical. Zotero was not contacted; no
  credentials or databases were touched.

## UI verification (manual, in-app browser)

Checked against live data: dashboard (providers, engine state, projects, usage), new-task
form (routing preview with reasons, mock scenario), live task page (phase timeline, live
events, fix loop indicator, approval card), runs table, diff, proposals side-by-side with
differences, agent control center (real provider details and model statuses), task board,
light/dark themes, mobile width (no horizontal page scroll). Bugs found and fixed during
this pass: pytest summary parsing, project card overflow on mobile, compare phase shown as
"skipped", the first comparison never pairing steps.
