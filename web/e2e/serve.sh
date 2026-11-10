#!/bin/sh
# Starts an isolated Forge server for e2e tests: fresh FORGE_HOME, mock provider on,
# and a disposable git repo the test imports through the UI.
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
E2E="${TMPDIR:-/tmp}/forge-e2e"
rm -rf "$E2E" && mkdir -p "$E2E/repo/tests"
cd "$E2E/repo"
printf 'def add(a, b):\n    return a + b\n' > calc.py
printf 'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n' > tests/test_calc.py
printf '[tool.pytest.ini_options]\npythonpath = ["."]\n' > pyproject.toml
git init -q -b main && git add -A
git -c user.name=e2e -c user.email=e2e@e2e commit -qm init
export FORGE_HOME="$E2E/home"
"$ROOT/.venv/bin/python" - <<'PY'
import asyncio
from forge.app import Forge
f = Forge.create(discover=False)
f.save_settings({"enable_mock_provider": True, "disabled_providers": ["claude_code", "codex"]})
f.registry.refresh()
asyncio.run(f.registry.probe_all())
PY
exec "$ROOT/.venv/bin/forge" serve --port 8799
