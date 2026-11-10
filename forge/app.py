"""Application container: wires the database, services, providers, router and engine.

Both the API server and the CLI construct a `Forge` against the same FORGE_HOME,
so they share state and the orchestration engine semantics.
"""

from __future__ import annotations

from .config import Paths, get_paths
from .db import Database
from .domain import GlobalSettings
from .events import EventBus
from .memory import MemoryService
from .services import ApprovalService, ArtifactService, ProjectService, TaskService


class Forge:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.db = Database(paths.db)
        self.bus = EventBus(self.db)
        self.projects = ProjectService(self.db, self.bus, paths.projects)
        self.projects.default_limits = lambda: self.settings().default_limits.model_dump()
        self.tasks = TaskService(self.db, self.bus, self.projects)
        self.artifacts = ArtifactService(self.db, self.bus)
        self.approvals = ApprovalService(self.db, self.bus)
        self.memory = MemoryService(self.db, self.bus)
        self.registry = None
        self.router = None
        self.engine = None

    @classmethod
    def create(cls, *, discover: bool = True) -> "Forge":
        f = cls(get_paths())
        f._wire(discover=discover)
        return f

    def _wire(self, *, discover: bool) -> None:
        from .engine import Engine
        from .providers.registry import ProviderRegistry
        from .router import Router

        self.registry = ProviderRegistry(self)
        self.router = Router(self)
        self.engine = Engine(self)
        if discover:
            self.registry.refresh(probe=False)

    # --- global settings -------------------------------------------------------
    def settings(self) -> GlobalSettings:
        return GlobalSettings.model_validate(self.db.get_setting("global", {}) or {})

    def save_settings(self, patch: dict) -> GlobalSettings:
        merged = {**self.settings().model_dump(), **patch}
        s = GlobalSettings.model_validate(merged)
        self.db.set_setting("global", s.model_dump())
        self.bus.emit("settings.updated", "Global settings updated", data={"keys": sorted(patch)})
        return s

    def close(self) -> None:
        self.db.close()
