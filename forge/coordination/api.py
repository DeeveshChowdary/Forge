"""Versioned, token-authenticated coordination API (/api/v1/coordination/*) and the
local UI endpoints that configure it (/api/coordination/*, protected by the
existing localhost + X-Forge-Client guard)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .bridge import Bridge
from .service import CoordinationError, CoordinationService, ExternalTaskRequest, Feedback
from .tokens import AuthError, Principal, RateLimiter, ScopeError, TokenService

if TYPE_CHECKING:
    from ..app import Forge


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    projects: list[str] = Field(min_length=1)


def build_routers(forge: "Forge", bridge: Bridge) -> tuple[APIRouter, APIRouter]:
    svc = CoordinationService(forge)
    tokens = TokenService(forge.db)
    limiter = RateLimiter(svc.settings().api_rate_per_minute)

    def principal(request: Request) -> Principal:
        p = tokens.authenticate(request.headers.get("authorization"))
        if not limiter.allow(p.token_id):
            raise _RateLimited()
        return p

    v1 = APIRouter(prefix="/api/v1/coordination", tags=["coordination"])

    @v1.get("/projects")
    def projects(p: Principal = Depends(principal)):
        return {"items": svc.projects(p)}

    @v1.get("/tasks")
    def tasks(p: Principal = Depends(principal), project: str | None = None, state: str | None = None,
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return svc.tasks(p, project=project, state=state, limit=limit, offset=offset)

    @v1.get("/tasks/{task_id}")
    def task(task_id: str, p: Principal = Depends(principal)):
        return svc.task(p, task_id)

    @v1.get("/tasks/{task_id}/events")
    def events(task_id: str, p: Principal = Depends(principal), after: int = Query(0, ge=0),
               limit: int = Query(100, ge=1, le=500)):
        return svc.events(p, task_id, after=after, limit=limit)

    @v1.get("/tasks/{task_id}/artifacts")
    def artifacts(task_id: str, p: Principal = Depends(principal), kind: str | None = None,
                  limit: int = Query(50, ge=1, le=200)):
        return svc.artifacts(p, task_id, kind=kind, limit=limit)

    @v1.get("/agents")
    def agents(p: Principal = Depends(principal)):
        return svc.agents(p)

    @v1.post("/tasks", status_code=201)
    def submit(body: ExternalTaskRequest, p: Principal = Depends(principal)):
        return svc.submit(p, body, source="api")

    @v1.post("/tasks/{task_id}/feedback")
    def feedback(task_id: str, body: Feedback, p: Principal = Depends(principal)):
        return svc.feedback(p, task_id, body, source="api")

    # ---------------------------------------------------------------- local UI endpoints
    ui = APIRouter(prefix="/api/coordination", tags=["coordination-ui"])

    @ui.get("/status")
    def status():
        return {**bridge.status(), "api_settings": svc.settings().model_dump(), "tokens": tokens.list()}

    @ui.put("/bridge")
    def configure(patch: dict[str, Any]):
        return bridge.save_config(patch).model_dump()

    @ui.put("/settings")
    def api_settings(patch: dict[str, Any]):
        return svc.save_settings(patch).model_dump()

    @ui.post("/sync")
    def sync():
        return bridge.sync_once()

    @ui.get("/links")
    def links():
        return bridge.links()

    @ui.get("/activity")
    def activity(limit: int = 100):
        rows = forge.db.query("SELECT * FROM events WHERE type LIKE 'coordination.%' ORDER BY id DESC LIMIT ?",
                              (min(limit, 500),))
        return rows

    @ui.post("/tokens")
    def create_token(body: TokenCreate):
        return tokens.create(body.name, body.scopes, body.projects)

    @ui.delete("/tokens/{tid}")
    def revoke(tid: str):
        tokens.revoke(tid)
        return {"ok": True}

    return v1, ui


class _RateLimited(Exception):
    pass


def install_handlers(app) -> None:
    @app.exception_handler(AuthError)
    async def _auth(_, e):
        return JSONResponse({"error": "unauthorized", "detail": str(e)}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(ScopeError)
    async def _scope(_, e):
        return JSONResponse({"error": "forbidden", "detail": str(e)}, status_code=403)

    @app.exception_handler(CoordinationError)
    async def _coord(_, e):
        return JSONResponse({"error": type(e).__name__.lower(), "detail": str(e)}, status_code=e.status)

    @app.exception_handler(_RateLimited)
    async def _rl(_, e):
        return JSONResponse({"error": "rate_limited", "detail": "too many requests for this token"},
                            status_code=429, headers={"Retry-After": "30"})
