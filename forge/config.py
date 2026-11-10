"""Filesystem locations and process-wide settings.

All state lives under FORGE_HOME (default ~/.forge) so that the repository
itself never holds runtime data, and tests can point Forge at a temp dir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def db(self) -> Path:
        return self.home / "forge.db"

    @property
    def worktrees(self) -> Path:
        return self.home / "worktrees"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def scratch(self) -> Path:
        return self.home / "scratch"

    @property
    def projects(self) -> Path:
        """Default location for projects created from scratch."""
        return self.home / "projects"

    def ensure(self) -> "Paths":
        for p in (self.home, self.worktrees, self.logs, self.scratch, self.projects):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> Paths:
    home = Path(os.environ.get("FORGE_HOME", Path.home() / ".forge")).expanduser()
    return Paths(home=home).ensure()


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("FORGE_PORT", "8765"))

# The web UI is served from here when built (`pnpm --dir web build`).
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
