"""HTTP API (FastAPI). The web UI is one client of this API; the CLI uses the
same services directly against the same database.

Security: binds to localhost; rejects foreign Host headers (DNS rebinding) and
requires a custom header on mutating requests so other websites cannot issue
cross-site requests to the local server.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, gitops, integrations
from . import workflows as wfmod
from .app import Forge
from .config import WEB_DIST
from .coordination.api import build_routers, install_handlers
from .coordination.bridge import Bridge
from .db import loads
from .domain import ROUTING_MODES, InvalidTransition, ProjectConfig
from .engine import submit_task
from .router import RouteContext, RoutingError
from .services import Conflict, NotFound

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


# ---------------------------------------------------------------- request models
class ProjectCreate(BaseModel):
    mode: str = Field("create", pattern="^(create|import_folder|import_git)$")
    name: str | None = None
    path: str | None = None
    url: str | None = None
    description: str = ""
    instructions: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    description: str | None = None
    instructions: str | None = None
    config: dict[str, Any] | None = None


class TaskCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    workflow: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    complexity: str = Field("medium", pattern="^(low|medium|high)$")
    criticality: str = Field("normal", pattern="^(low|normal|high)$")
    routing_mode: str | None = None
    routing_overrides: dict[str, Any] = Field(default_factory=dict)
    review_depth: str = Field("standard", pattern="^(none|standard|independent)$")
    approval_policy: str = Field("risky", pattern="^(risky|every_phase|never_merge)$")
    execution_mode: str | None = Field(None, pattern="^(worktree|read_only)$")
    budget: dict[str, Any] = Field(default_factory=dict)
    decompose: bool = False
    as_milestone: bool = False
    fanout_count: int | None = Field(None, ge=1, le=4)
    mock: dict[str, Any] | None = None


class Decision(BaseModel):
    approve: bool
    note: str = ""


class MemoryCreate(BaseModel):
    kind: str
    title: str
    content: str
    importance: float = 0.6
    shared: bool = False
    pinned: bool = False


class IntegrationPut(BaseModel):
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)


class ModelPatch(BaseModel):
    enabled: bool | None = None
    preference: float | None = Field(None, ge=-1, le=1)
    profile: dict[str, Any] | None = None


class ModelAdd(BaseModel):
    provider: str
    model_id: str
    display_name: str = ""


class RoutePreview(BaseModel):
    project_id: str
    phase: str = "implement"
    role: str = "implementer"
    complexity: str = "medium"
    criticality: str = "normal"
    mode: str = "balanced"


def create_app(forge: Forge | None = None, *, run_engine: bool = True) -> FastAPI:
    forge = forge or Forge.create()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        bridge_task = asyncio.create_task(bridge.run_forever())  # no-op until enabled in settings
        if run_engine:
            task = asyncio.create_task(forge.engine.run_forever())
        yield
        bridge.stop()
        bridge_task.cancel()
        if task:
            forge.engine.stop()
            try:
                await asyncio.wait_for(task, 15)
            except asyncio.TimeoutError:
                task.cancel()

    bridge = Bridge(forge)
    app = FastAPI(title="Forge", version=__version__, lifespan=lifespan)
    app.state.forge = forge
    app.state.bridge = bridge

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/v1/"):
            # Token-authenticated API: bearer tokens are never sent automatically by browsers, so the
            # X-Forge-Client header is not required; browser-originated cross-site calls are still refused.
            if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in ALLOWED_HOSTS:
                return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
            return await call_next(request)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            if request.headers.get("x-forge-client") is None:
                return JSONResponse({"detail": "missing X-Forge-Client header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in ALLOWED_HOSTS:
                return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        return await call_next(request)

    v1, coord_ui = build_routers(forge, bridge)
    app.include_router(v1)
    app.include_router(coord_ui)
    install_handlers(app)

    @app.exception_handler(NotFound)
    async def _nf(_, e):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(Conflict)
    async def _cf(_, e):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(InvalidTransition)
    async def _it(_, e):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def _pe(_, e):
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(_, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(RoutingError)
    async def _re(_, e):
        return JSONResponse({"detail": str(e)}, status_code=422)

    # ------------------------------------------------------------ system
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "engine": forge.engine.lock_info(),
                "engine_is_self": forge.engine.is_owner, "home": str(forge.paths.home)}

    @app.get("/api/overview")
    def overview():
        projects = forge.projects.list()
        active = forge.tasks.list(states=["PLANNING", "EXECUTING", "TESTING", "REVIEWING"], limit=50)
        queued = forge.tasks.list(states=["READY", "AWAITING_APPROVAL", "BLOCKED", "PAUSED"], limit=50)
        recent = forge.tasks.list(states=["COMPLETED", "FAILED", "CANCELLED"], limit=15)
        pending = forge.approvals.list(status="pending")
        names = {p["id"]: p["name"] for p in projects}
        for t in active + queued + recent:
            t["project_name"] = names.get(t["project_id"])
        running_runs = forge.db.query(
            "SELECT r.id, r.task_id, r.phase, r.role, r.provider, r.model, r.effort, r.started_at, t.title "
            "FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.state='running' ORDER BY r.started_at")
        return {"projects": projects, "active": active, "queued": queued, "recent": recent,
                "approvals": pending, "running_runs": running_runs, "usage": usage_summary(),
                "providers": [{k: s.get(k) for k in ("provider", "display_name", "installed", "auth_state", "billing",
                                                     "is_mock", "version")} for s in forge.registry.statuses()],
                "engine": {"owner": forge.engine.is_owner, "lock": forge.engine.lock_info(),
                           "running": list(forge.engine.running)}}

    @app.get("/api/settings")
    def get_settings():
        return {"settings": forge.settings().model_dump(), "routing_modes": ROUTING_MODES,
                "workflows": wfmod.describe(), "default_project_config": ProjectConfig().model_dump()}

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any]):
        s = forge.save_settings(patch)
        forge.registry.reload()
        return s.model_dump()

    @app.get("/api/workflows")
    def workflows():
        return wfmod.describe()

    # ------------------------------------------------------------ providers & models
    @app.get("/api/providers")
    def providers():
        return {"providers": forge.registry.statuses(), "stats": forge.router.model_stats()}

    @app.post("/api/providers/refresh")
    def refresh_providers():
        return forge.registry.refresh(probe=False)

    @app.post("/api/models/probe")
    async def probe_model(provider: str = Query(...), model_id: str = Query(...)):
        return await forge.registry.probe(provider, model_id)

    @app.post("/api/models/probe-all")
    async def probe_all(only_unverified: bool = True):
        return await forge.registry.probe_all(only_unverified=only_unverified)

    @app.patch("/api/models")
    def patch_model(body: ModelPatch, provider: str = Query(...), model_id: str = Query(...)):
        return forge.registry.update_model(provider, model_id, enabled=body.enabled, preference=body.preference,
                                           profile=body.profile)

    @app.post("/api/models")
    def add_model(body: ModelAdd):
        return forge.registry.add_configured_model(body.provider, body.model_id, body.display_name)

    @app.post("/api/router/preview")
    def route_preview(body: RoutePreview):
        if body.mode not in ROUTING_MODES or body.mode == "manual":
            raise ValueError("preview supports balanced, max_quality, economy")
        d = forge.router.route(RouteContext(project_id=body.project_id, task_id="preview", phase=body.phase,
                                            role=body.role, complexity=body.complexity,
                                            criticality=body.criticality, mode=body.mode), persist=False)
        return d.__dict__

    # ------------------------------------------------------------ projects
    @app.get("/api/projects")
    def list_projects():
        return forge.projects.list()

    @app.post("/api/projects")
    def create_project(body: ProjectCreate):
        if body.mode == "create":
            if not body.name:
                raise ValueError("name is required")
            return forge.projects.create(body.name, body.description, body.path, body.config, body.instructions)
        if body.mode == "import_folder":
            if not body.path:
                raise ValueError("path is required")
            return forge.projects.import_folder(body.path, body.name, body.description, body.config, body.instructions)
        if not (body.url or body.path):
            raise ValueError("url is required")
        return forge.projects.import_git(body.url or body.path, body.name, body.description, body.config)

    @app.get("/api/projects/suggest")
    def suggest(path: str):
        from .services import suggest_test_command
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("not a directory")
        is_git = gitops.is_repo(root)
        return {"path": str(root), "name": root.name, "is_git": is_git,
                "has_commits": is_git and gitops.has_commits(root),
                "suggested_test_command": suggest_test_command(root),
                "instruction_files": [f for f in ("AGENTS.md", "CLAUDE.md") if (root / f).is_file()]}

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = forge.projects.get(pid)
        root = Path(p["root_path"])
        p["git"] = integrations.GitIntegration().status(forge, p, {}) if root.is_dir() else {"ok": False}
        p["instruction_files"] = forge.projects.instruction_files(p["id"])
        p["milestones"] = forge.projects.milestones(p["id"])
        p["usage"] = usage_summary(p["id"])
        return p

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, body: ProjectPatch):
        return forge.projects.update(pid, description=body.description, instructions=body.instructions,
                                     config=body.config)

    @app.post("/api/projects/{pid}/pause")
    def pause_project(pid: str):
        return forge.projects.set_status(pid, "paused")

    @app.post("/api/projects/{pid}/resume")
    def resume_project(pid: str):
        return forge.projects.set_status(pid, "active")

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        forge.projects.remove(pid)
        return {"ok": True, "note": "unregistered; project files were not deleted"}

    @app.get("/api/projects/{pid}/files")
    def files(pid: str, path: str = ""):
        p = forge.projects.get(pid)
        return integrations.list_dir(Path(p["root_path"]), path)

    @app.get("/api/projects/{pid}/file")
    def file(pid: str, path: str):
        p = forge.projects.get(pid)
        return integrations.read_file(Path(p["root_path"]), path)

    @app.get("/api/projects/{pid}/commits")
    def commits(pid: str, n: int = 30):
        p = forge.projects.get(pid)
        return gitops.log(p["root_path"], n) if gitops.is_repo(p["root_path"]) else []

    @app.get("/api/projects/{pid}/runs")
    def project_runs(pid: str, limit: int = 100):
        p = forge.projects.get(pid)
        rows = forge.db.query(
            "SELECT r.id, r.task_id, r.phase, r.role, r.kind, r.provider, r.model, r.resolved_model, r.effort, r.state, "
            "r.attempt, r.review_cycle, r.session_id, r.usage, r.error, r.started_at, r.ended_at, t.title "
            "FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.project_id=? ORDER BY r.started_at DESC LIMIT ?",
            (p["id"], limit))
        for r in rows:
            r["usage"] = loads(r["usage"], {})
            r["is_mock"] = r["provider"] == "mock"
        return rows

    @app.get("/api/projects/{pid}/artifacts")
    def project_artifacts(pid: str, kind: str | None = None, limit: int = 100):
        p = forge.projects.get(pid)
        rows = forge.artifacts.list(project_id=p["id"], kind=kind, limit=1000)[-limit:]
        for r in rows:
            r["content"] = r["content"][:4000]
        return list(reversed(rows))

    @app.post("/api/projects/{pid}/instructions/scan")
    def scan_instr(pid: str):
        return forge.projects.scan_instruction_files(pid)

    @app.post("/api/projects/{pid}/instructions/trust")
    def trust_instr(pid: str, path: str, trusted: bool = True):
        forge.projects.trust_instruction_file(pid, path, trusted)
        return forge.projects.instruction_files(pid)

    @app.get("/api/projects/{pid}/integrations")
    def get_integrations(pid: str):
        return integrations.describe(forge, pid)

    @app.put("/api/projects/{pid}/integrations/{typ}")
    def put_integration(pid: str, typ: str, body: IntegrationPut):
        if typ not in integrations.REGISTRY:
            raise NotFound(f"unknown integration {typ}")
        forge.projects.set_integration(pid, typ, body.enabled, body.config)
        return integrations.describe(forge, pid)

    @app.post("/api/projects/{pid}/integrations/{typ}/actions/{action}")
    def integration_action(pid: str, typ: str, action: str, args: dict[str, Any]):
        p = forge.projects.get(pid)
        row = next((r for r in forge.projects.integrations(p["id"]) if r["type"] == typ), None)
        if not row or not row["enabled"]:
            raise PermissionError(f"integration {typ} is not enabled for this project")
        return integrations.REGISTRY[typ].run_action(forge, p, row["config"], action, args)

    @app.get("/api/projects/{pid}/memory")
    def memory(pid: str, q: str = "", kind: str | None = None):
        p = forge.projects.get(pid)
        if q:
            return forge.memory.retrieve(p["id"], q, k=20, char_budget=20000,
                                         include_shared=p["config"].get("share_memory", False))
        return forge.memory.list(p["id"], kind)

    @app.post("/api/projects/{pid}/memory")
    def add_memory(pid: str, body: MemoryCreate):
        p = forge.projects.get(pid)
        return forge.memory.add(p["id"], body.kind, body.title, body.content, source="user (web UI)",
                                importance=body.importance, shared=body.shared, pinned=body.pinned)

    @app.delete("/api/memory/{mid}")
    def delete_memory(mid: str):
        forge.memory.delete(mid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/milestones")
    def add_milestone(pid: str, body: dict[str, str]):
        return forge.projects.add_milestone(pid, body.get("title", "Milestone"), body.get("description", ""))

    # ------------------------------------------------------------ tasks
    @app.get("/api/tasks")
    def list_tasks(project_id: str | None = None, state: list[str] | None = Query(None), limit: int = 200):
        return forge.tasks.list(project_id, state, limit)

    @app.post("/api/tasks")
    def create_task(body: TaskCreate):
        meta: dict[str, Any] = {}
        if body.decompose:
            meta["decompose"] = True
        if body.fanout_count:
            meta["fanout_count"] = body.fanout_count
        if body.mock is not None:
            if not forge.settings().enable_mock_provider:
                raise PermissionError("mock scenarios require the mock provider to be enabled in settings")
            meta["mock"] = body.mock
        return submit_task(forge, body.project_id, title=body.title, objective=body.objective, workflow=body.workflow,
                           acceptance_criteria=body.acceptance_criteria, depends_on=body.depends_on,
                           complexity=body.complexity, criticality=body.criticality, routing_mode=body.routing_mode,
                           routing_overrides=body.routing_overrides, review_depth=body.review_depth,
                           approval_policy=body.approval_policy, execution_mode=body.execution_mode,
                           budget=body.budget, metadata=meta, as_milestone=body.as_milestone)

    @app.get("/api/tasks/{tid}")
    def get_task(tid: str):
        t = forge.tasks.get(tid)
        t["project"] = {k: v for k, v in forge.projects.get(t["project_id"]).items() if k in ("id", "name", "root_path")}
        runs = forge.db.query("SELECT * FROM runs WHERE task_id=? ORDER BY started_at", (tid,))
        for r in runs:
            r["usage"] = loads(r["usage"], {})
            r["structured_output"] = loads(r["structured_output"])
            r["prompt_chars"] = len(r.pop("prompt") or "")
            r["output_text"] = (r["output_text"] or "")[-8000:]
            r["is_mock"] = r["provider"] == "mock"
        t["runs"] = runs
        t["artifacts"] = forge.artifacts.list(task_id=tid)
        t["routing"] = forge.router.decisions(tid)
        t["approvals"] = forge.approvals.list(task_id=tid)
        t["children"] = forge.tasks.list(parent_id=tid)
        t["events"] = forge.bus.recent(task_id=tid, limit=400)
        return t

    @app.get("/api/tasks/{tid}/diff")
    def task_diff(tid: str):
        t = forge.tasks.get(tid)
        if not t["base_commit"] or not t["worktree_path"] or not Path(t["worktree_path"]).is_dir():
            return PlainTextResponse("")
        return PlainTextResponse(gitops.diff(t["worktree_path"], t["base_commit"], "HEAD"))

    @app.get("/api/runs/{rid}/log")
    def run_log(rid: str, tail: int = 400):
        r = forge.db.one("SELECT log_path, prompt FROM runs WHERE id=?", (rid,))
        if not r:
            raise NotFound(rid)
        lines = []
        if r["log_path"] and Path(r["log_path"]).is_file():
            lines = Path(r["log_path"]).read_text(errors="replace").splitlines()[-tail:]
        return {"lines": lines, "prompt": r["prompt"]}

    @app.post("/api/tasks/{tid}/pause")
    def pause(tid: str):
        return forge.tasks.request_pause(tid)

    @app.post("/api/tasks/{tid}/resume")
    def resume(tid: str):
        return forge.tasks.resume(tid)

    @app.post("/api/tasks/{tid}/cancel")
    def cancel(tid: str):
        return forge.tasks.request_cancel(tid)

    @app.post("/api/tasks/{tid}/revise")
    def revise(tid: str, body: dict[str, str]):
        return forge.engine.request_revision(tid, body.get("note", ""))

    @app.post("/api/tasks/{tid}/retry")
    def retry(tid: str):
        return forge.tasks.retry(tid)

    # ------------------------------------------------------------ approvals
    @app.get("/api/approvals")
    def approvals(status: str | None = "pending", project_id: str | None = None):
        return forge.approvals.list(status=status, project_id=project_id)

    @app.post("/api/approvals/{aid}")
    def decide(aid: str, body: Decision):
        return forge.engine.apply_approval(aid, body.approve, body.note)

    # ------------------------------------------------------------ events / usage
    @app.get("/api/events")
    def events(after: int = 0, project_id: str | None = None, task_id: str | None = None, limit: int = 200):
        if after:
            return forge.bus.since(after, project_id=project_id, task_id=task_id, limit=limit)
        return forge.bus.recent(project_id=project_id, task_id=task_id, limit=limit)

    @app.get("/api/events/stream")
    async def stream(request: Request, after: int | None = None, project_id: str | None = None,
                     task_id: str | None = None):
        last = forge.bus.last_id() if after is None else after

        async def gen():
            nonlocal last
            yield f"event: hello\ndata: {json.dumps({'last_id': last})}\n\n"
            beat = time.monotonic()
            while not await request.is_disconnected():
                rows = forge.bus.since(last, project_id=project_id, task_id=task_id, limit=200)
                for r in rows:
                    last = r["id"]
                    yield f"id: {r['id']}\nevent: forge\ndata: {json.dumps(r, default=str)}\n\n"
                if not rows:
                    await forge.bus.wait(0.75)
                if time.monotonic() - beat > 15:
                    beat = time.monotonic()
                    yield ": keepalive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def usage_summary(project_id: str | None = None) -> dict:
        sql = "SELECT provider, model, resolved_model, usage, state, started_at, ended_at FROM runs WHERE kind='agent'"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        per: dict[str, dict] = {}
        for r in forge.db.query(sql, params):
            u = loads(r["usage"], {})
            key = r["provider"] or "unknown"
            d = per.setdefault(key, {"provider": key, "runs": 0, "succeeded": 0, "input_tokens": 0, "output_tokens": 0,
                                     "cached_input_tokens": 0, "billed_usd": 0.0, "subscription_equivalent_usd": 0.0,
                                     "runs_without_token_data": 0, "billing": set(), "seconds": 0.0})
            d["runs"] += 1
            d["succeeded"] += r["state"] == "succeeded"
            if u.get("input_tokens") is None:
                d["runs_without_token_data"] += 1
            for k in ("input_tokens", "output_tokens", "cached_input_tokens"):
                d[k] += u.get(k) or 0
            if u.get("cost_usd") is not None:
                if u.get("cost_kind") == "billed":
                    d["billed_usd"] += u["cost_usd"]
                elif u.get("cost_kind") == "subscription_equivalent":
                    d["subscription_equivalent_usd"] += u["cost_usd"]
            if u.get("billing"):
                d["billing"].add(u["billing"])
            if r["ended_at"]:
                d["seconds"] += r["ended_at"] - r["started_at"]
        for d in per.values():
            d["billing"] = sorted(d["billing"])
            d["billed_usd"] = round(d["billed_usd"], 4)
            d["subscription_equivalent_usd"] = round(d["subscription_equivalent_usd"], 4)
        return {"per_provider": list(per.values()),
                "note": "Billed USD counts only directly billed API usage (none unless enabled). Subscription-equivalent "
                        "is a provider-reported list-price estimate for subscription usage and is not charged. Codex "
                        "reports tokens only."}

    @app.get("/api/usage")
    def usage(project_id: str | None = None):
        return usage_summary(project_id)

    # ------------------------------------------------------------ SPA
    if WEB_DIST.is_dir():
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404)
            f = (WEB_DIST / full_path).resolve()
            if full_path and f.is_file() and f.is_relative_to(WEB_DIST.resolve()):
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_ui():
            return PlainTextResponse("Forge API is running. The web UI is not built: run `pnpm --dir web install && "
                                     "pnpm --dir web build`, then restart `forge serve`.")

    return app
