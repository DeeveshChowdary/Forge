# Forge — Claude Code entry point

Read and follow [AGENTS.md](AGENTS.md) (rules and checks), then
[ARCHITECTURE.md](ARCHITECTURE.md) and [HANDOFF.md](HANDOFF.md) (current state and next
step). Security properties are listed in [SECURITY.md](SECURITY.md); do not weaken them.

Quick commands:

```bash
.venv/bin/pytest -q                     # backend tests
pnpm --dir web test                     # UI component tests
pnpm --dir web build                    # build UI served by `forge serve`
FORGE_HOME=$PWD/.forge-local/dev .venv/bin/forge serve --port 8766   # dev server, isolated data
```

When Claude Code itself is launched by Forge as an agent, Forge passes
`--setting-sources user`, so this file is read only as repository context, not as trusted
Forge instructions.
