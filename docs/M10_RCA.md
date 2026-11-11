# M10 — Root cause: task t_fa827e885e2d ("R4 M1 — Deep Prior-Art Review")

Evidence: `~/.forge/forge.db` (runs, routing_decisions, events, artifacts), read-only.

## What happened

| Phase | Agent | Result | Steps | Time |
|---|---|---|---|---|
| research | claude_code / claude-sonnet-5-5 (medium) | succeeded — structured report | 29 | 78 s |
| hypothesis | claude_code / claude-sonnet-5-5 (medium) | succeeded — structured report | 3 | 29 s |
| experiment #1 | claude_code / claude-sonnet-5-5 (medium) | **failed: turn limit 40 exceeded** | 41 | 226 s |
| experiment #2 | claude_code / sonnet → claude-sonnet-4-6 (medium) | **failed: turn limit 40 exceeded** | 41 | 392 s |

No permission denials. Task ended FAILED; analyze never ran.

## Why it exceeded the limit

1. **Wrong workflow for the work.** The objective was literature-only (prior art, novelty,
   decision), but it ran in `research`, whose `experiment` phase instructs the agent to
   "implement the minimal experiment … record raw results". Attempt 1 spent its steps on
   environment checks (≈18 shell calls: git, python/pytest versions, `import pulp`) and
   started building `experiments/m1_planner_sim/planner_sim.py`.
2. **Excessive scope in one phase.** The 5,940-character objective had 7 sub-phases
   (environment verification, Zotero, path fixes, reading, two independent
   investigations, novelty, decision). All deliverables landed in one 40-step phase.
3. **Identical retry after a deterministic limit.** Attempt 2 restarted from scratch
   (partial work had been reset), re-read ~20 files, wrote 3 deliverables and was cut off
   at step 41 while writing more. It could not succeed with the same limit.
4. **Limit too low and global.** 40 steps applied to every phase regardless of its role.

## Why Codex did not participate

Routing only. Mode `balanced` + complexity `medium` → required tier standard; both
providers scored ≈0.30, and a small catalog-order tie-breaker kept Claude first (attempt 2:
Claude 0.30 vs Codex `gpt-6.1-sol` 0.29). The objective's "Claude: … / Codex: …" split was
plain text; Forge had no collaboration policy to enforce it.

## Useful research completed

* Two structured reports (research, hypothesis) — complete, in the task's artifacts.
* Attempt 2 partial drafts (+591 lines): `literature/reviews/deep_review.md`,
  `proposals/formal_model.md`, `proposals/research_hypotheses.md` — unreviewed, cut off.
* Attempt 1 partial work (+516 lines): an executable planner simulation and pre-registration
  — out of scope for a literature milestone and contrary to R4's "no runtime without a
  reviewed decision" rule.

Side finding: the agent read files outside its worktree (`~/.claude/projects/…/MEMORY.md`,
listing of the original `~/research/r4-computation-migration`). Claude Code's `Read` is not
confined to the working directory; read-only, but noted as a limitation.

## Fixes (M10)

* `literature_review` workflow (no experiment phase; reports written and committed by Forge
  with citation checks; GO/PIVOT/REJECT/INCONCLUSIVE decision).
* Collaboration policies enforced in the engine (`independent_research` is the default for
  `literature_review`); unsatisfiable policies block instead of falling back.
* Per-phase step/runtime limits (literature phases 60–100 steps), preflight warnings that
  flag exactly this objective, section decomposition.
* Partial work is preserved as checkpoint refs + diffs; after a limit hit Forge continues
  once from the checkpoint with the same agent and session, then stops with a clear reason;
  identical retries are refused.
* The task page shows failure analysis, recoverable work and retry eligibility (verified on
  this task's real data).

## Recovery plan — needs your approval before anything touches the research repo

Staged (outside the repo) in `~/.forge/recovery/t_fa827e885e2d/`:
`attempt2-deep-review.patch`, `attempt1-planner-sim.patch`, `research-phase-report.json`,
`hypothesis-phase-report.json`. Both patches were verified with `git apply --check` against
the task's base commit `8a48c09` (clean, non-overlapping).

Recommended:
1. Recover **attempt 2** (deep review, formal model, hypotheses) to a new branch
   `forge/recovery-t_fa827e885e2d-1` via Forge (task page → Recoverable work → Recover to a
   new branch → approve). `main` and your working tree are not touched.
2. Do **not** recover attempt 1 into the research line (executable experiment, out of scope).
3. Treat recovered files as **unreviewed drafts**: they were cut off mid-write and their
   citations have not been checked. Use them as input to a new `literature_review` task
   (`independent_research` policy) rather than as findings.
