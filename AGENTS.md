# Instructions for coding agents working on Forge

Forge is a local-first multi-model engineering harness: Python backend in `forge/`,
React UI in `web/`. Read `ARCHITECTURE.md` first, then `HANDOFF.md` for current state.

## Ground rules

* Never commit secrets, provider credentials, or anything from `~/.forge`, `~/.claude`,
  `~/.codex`. Never modify the user's authentication.
* Never push, publish, or create remote repositories. Local commits only when asked.
* Do not weaken safety properties documented in `SECURITY.md` (env scrubbing, read-only
  sandboxes, worktree isolation, approval gates, CSRF/host guards, instruction trust).
* Do not claim a model, provider or feature works without a test or a real run showing it.
  Mock-provider output must stay clearly labelled.
* Do not hardcode model IDs as permanent truths. Discover, configure, then verify by probe.

## Working on the code

* Task state changes only via `TaskService.transition`; add new transitions to
  `domain.TRANSITIONS` deliberately.
* Engine changes must keep: single engine lock, task leases, atomic phase completion,
  bounded review/fix loops, recovery without duplicate execution. Add a test in
  `tests/test_engine.py` for any new path.
* External processes: argv lists, `providers.proc.clean_env()`, timeouts, process groups.
* Routing changes: keep reasons human-readable and persisted; update `tests/test_router.py`.

## Checks before finishing

```bash
.venv/bin/pytest -q
pnpm --dir web exec tsc --noEmit -p . && pnpm --dir web test && pnpm --dir web build
pnpm --dir web exec playwright test
```

Update `HANDOFF.md` (state, remaining work, exact next step) and `TEST_RESULTS.md` when
you change behaviour or run new verification.
