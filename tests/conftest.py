import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def forge_home(tmp_path, monkeypatch):
    home = tmp_path / "forge_home"
    monkeypatch.setenv("FORGE_HOME", str(home))
    return home


@pytest.fixture
def app(forge_home):
    from forge.app import Forge

    f = Forge.create(discover=False)
    yield f
    f.close()


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    for rel, content in (files or {"README.md": "# demo\n"}).items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


CALC_FILES = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    "pyproject.toml": "[tool.pytest.ini_options]\npythonpath = [\".\"]\n",
}


@pytest.fixture
def py_repo(tmp_path):
    return make_repo(tmp_path / "calc_project", CALC_FILES)
