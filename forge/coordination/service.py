"""Coordination service: the single, principal-scoped facade used by the v1 API,
the GitHub bridge and the MCP adapter.

It reuses Forge's existing services and engine (submit_task, approvals,
request_revision, event bus, artifacts). It never invents progress: every field
is read from persisted task, run, artifact and event rows. Everything returned
is sanitized; local paths, prompts, raw logs and credentials are never exposed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import workflows as wfmod
from ..db import loads
from ..domain import ACTIVE, TERMINAL, TaskState
from ..engine import submit_task
from .sanitize import clean_inbound, sanitize
from .tokens import Principal

if TYPE_CHECKING:
    from ..app import Forge

PUBLIC_ARTIFACT_KINDS = {"plan", "handoff", "review", "report", "proposal", "comparison", "test_report", "diff",
                         "revision_request", "external_feedback"}
SAFE_EVENT_DATA = {"from", "to", "provider", "model", "phase", "passed", "state", "kind", "exit_code", "mock",
                   "resolved_model", "effort"}
HIDDEN_EVENT_TYPES = {"agent.raw", "agent.thinking"}


class CoordinationSettings(BaseModel):
    # Externally submitted tasks (API, MCP, GitHub) wait for the user's approval in Forge before they run.
    require_authorization: bool = True
    allowed_workflows: list[str] = Field(default_factory=lambda: sorted(wfmod.WORKFLOWS))
    api_rate_per_minute: int = 120


class ExternalTaskRequest(BaseModel):
    """Schema for tasks submitted by external assistants (API, MCP and GitHub issues)."""

    model_config = ConfigDict(extra="forbid")
    project: str = Field(min_length=1, max_length=100, description="Forge project name or id")
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    workflow: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    complexity: Literal["low", "medium", "high"] = "medium"
    routing_mode: Literal["balanced", "max_quality", "economy"] = "balanced"
    review_depth: Literal["standard", "independent"] = "standard"
    approval_policy: Literal["risky", "never_merge"] = "risky"
    read_only: bool = False
    collaboration: Literal["auto", "cross_model", "independent_research", "implement_review"] | None = None


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["revision", "comment"]
    message: str = Field(min_length=1, max_length=4000)


class CoordinationError(Exception):
    status = 400


class NotAllowed(CoordinationError):
    status = 403


class Missing(CoordinationError):
    status = 404


class Conflict(CoordinationError):
    status = 409


def iso(t: float | None) -> str | None:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds") if t else None


class CoordinationService:
    def __init__(self, forge: "Forge"):
        self.forge = forge

    # ------------------------------------------------------------- settings
    def settings(self) -> CoordinationSettings:
        return CoordinationSettings.model_validate(self.forge.db.get_setting("coordination_api", {}) or {})

    def save_settings(self, patch: dict) -> CoordinationSettings:
        s = CoordinationSettings.model_validate({**self.settings().model_dump(), **patch})
        self.forge.db.set_setting("coordination_api", s.model_dump())
        return s

    # ------------------------------------------------------------- helpers
    def _project(self, principal: Principal, ref: str) -> dict:
        try:
            p = self.forge.projects.get(ref)
        except Exception:  # noqa: BLE001 - do not reveal whether an unauthorized project exists
            raise Missing(f"project {ref!r} not found")
        if not principal.can_access(p["id"]):
            raise Missing(f"project {ref!r} not found")
        return p

    def _task(self, principal: Principal, tid: str) -> dict:
        try:
            t = self.forge.tasks.get(tid)
        except Exception:  # noqa: BLE001
            raise Missing(f"task {tid} not found")
        if not principal.can_access(t["project_id"]):
            raise Missing(f"task {tid} not found")
        return t

    def _roots(self, t: dict) -> list[str]:
        p = self.forge.projects.get(t["project_id"])
        return [r for r in (t.get("worktree_path"), p["root_path"]) if r]

    def _s(self, text: Any, t: dict | None = None, n: int = 4000) -> str:
        return sanitize(str(text) if text is not None else "", roots=self._roots(t) if t else None, max_len=n)

    # ------------------------------------------------------------- reads
    def projects(self, principal: Principal) -> list[dict]:
        out = []
        for p in self.forge.projects.list():
            if not principal.can_access(p["id"]):
                continue
            out.append({"id": p["id"], "name": p["name"], "description": sanitize(p["description"], max_len=500),
                        "status": p["status"], "permission_mode": p["config"]["permissions"]["mode"],
                        "default_workflow": p["config"]["default_workflow"],
                        "has_test_command": bool(p["config"]["test_command"]), "task_counts": p["task_counts"]})
        return out

    def task_view(self, t: dict, *, detail: bool = False) -> dict:
        p = self.forge.projects.get(t["project_id"])
        runs = self.forge.db.query(
            "SELECT id, phase, role, kind, provider, model, resolved_model, effort, state, attempt, review_cycle, "
            "usage, error, started_at, ended_at FROM runs WHERE task_id=? ORDER BY started_at", (t["id"],))
        current = next((r for r in reversed(runs) if r["state"] == "running"), None)
        last_agent = next((r for r in reversed(runs) if r["kind"] == "agent"), None)
        tests = self.forge.artifacts.latest(t["id"], "test_report")
        review = self.forge.artifacts.latest(t["id"], "review")
        pending = self.forge.approvals.list(task_id=t["id"], status="pending")
        v: dict[str, Any] = {
            "id": t["id"], "title": sanitize(t["title"], max_len=200),
            "project": {"id": p["id"], "name": p["name"]},
            "workflow": t["workflow"], "state": t["state"],
            "phase": None if (t["phase"] or "").startswith("__") else t["phase"],
            "is_active": TaskState(t["state"]) in ACTIVE, "is_terminal": TaskState(t["state"]) in TERMINAL,
            "routing_mode": t["routing_mode"], "review_cycle": t["review_cycle"],
            "max_review_cycles": t["budget"].get("max_review_cycles"),
            "assigned": ({"provider": (current or last_agent)["provider"], "model": (current or last_agent)["model"],
                          "resolved_model": (current or last_agent)["resolved_model"],
                          "effort": (current or last_agent)["effort"], "running": current is not None}
                         if (current or last_agent) else None),
            "created_at": iso(t["created_at"]), "started_at": iso(t["started_at"]),
            "updated_at": iso(t["updated_at"]), "finished_at": iso(t["finished_at"]),
            "disposition": self._s(t["disposition"], t, 500) or None,
            "error": self._s(t["error"], t, 1000) or None,
            "tests": ({"passed": tests["data"].get("passed"), "summary": self._s(tests["data"].get("summary"), t, 300),
                       "skipped": bool(tests["data"].get("skipped"))} if tests and tests.get("data") else None),
            "review": ({"verdict": review["data"].get("verdict"),
                        "summary": self._s(review["data"].get("summary"), t, 1500),
                        "issues": [{"severity": i.get("severity"), "file": self._s(i.get("file"), t, 200),
                                    "description": self._s(i.get("description"), t, 600)}
                                   for i in (review["data"].get("issues") or [])[:15]]}
                       if review and review.get("data") else None),
            "pending_approvals": [{"id": a["id"], "kind": a["kind"], "title": self._s(a["title"], t, 200),
                                   "decided_by": "human in Forge"} for a in pending],
            "source": t["metadata"].get("source", "forge"),
            "source_ref": t["metadata"].get("source_ref"),
        }
        if detail:
            v["objective"] = self._s(t["objective"], t, 8000)
            v["acceptance_criteria"] = [self._s(c, t, 500) for c in t["acceptance_criteria"]]
            v["runs"] = [{"id": r["id"], "phase": r["phase"], "role": r["role"], "kind": r["kind"],
                          "provider": r["provider"], "model": r["model"], "resolved_model": r["resolved_model"],
                          "effort": r["effort"], "state": r["state"], "attempt": r["attempt"],
                          "review_cycle": r["review_cycle"], "started_at": iso(r["started_at"]),
                          "ended_at": iso(r["ended_at"]),
                          "duration_s": round(r["ended_at"] - r["started_at"], 1) if r["ended_at"] else None,
                          "tokens": {k: loads(r["usage"], {}).get(k) for k in ("input_tokens", "output_tokens")},
                          "error": self._s(r["error"], t, 500) or None} for r in runs]
            v["commits"] = self._commits(t)
        return v

    def _commits(self, t: dict) -> list[dict]:
        from .. import gitops
        if not t.get("base_commit") or not t.get("worktree_path") or not Path(t["worktree_path"]).is_dir():
            return []
        try:
            return [{"sha": c["sha"], "short": c["short"], "subject": sanitize(c["subject"], max_len=200)}
                    for c in gitops.log(t["worktree_path"], 30, f"{t['base_commit']}..HEAD")]
        except Exception:  # noqa: BLE001
            return []

    def tasks(self, principal: Principal, *, project: str | None = None, state: str | None = None,
              limit: int = 50, offset: int = 0) -> dict:
        limit = max(1, min(limit, 200))
        sql, params = "SELECT id FROM tasks WHERE 1=1", []
        if project:
            p = self._project(principal, project)
            sql += " AND project_id=?"
            params.append(p["id"])
        if state:
            sql += " AND state=?"
            params.append(state.upper())
        rows = [r for r in self.forge.db.query(sql + " ORDER BY created_at DESC", params)]
        visible = [r["id"] for r in rows if principal.can_access(self.forge.tasks.get(r["id"])["project_id"])]
        page = visible[offset: offset + limit]
        return {"items": [self.task_view(self.forge.tasks.get(i)) for i in page], "total": len(visible),
                "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(visible) else None}

    def task(self, principal: Principal, tid: str) -> dict:
        return self.task_view(self._task(principal, tid), detail=True)

    def events(self, principal: Principal, tid: str, *, after: int = 0, limit: int = 100) -> dict:
        t = self._task(principal, tid)
        limit = max(1, min(limit, 500))
        rows = self.forge.bus.since(after, task_id=t["id"], limit=limit)
        items = [{"id": e["id"], "ts": iso(e["ts"]), "type": e["type"], "level": e["level"],
                  "run_id": e["run_id"], "message": self._s(e["message"], t, 2000),
                  "data": {k: v for k, v in (e["data"] or {}).items() if k in SAFE_EVENT_DATA}}
                 for e in rows if e["type"] not in HIDDEN_EVENT_TYPES]
        return {"items": items, "next_after": rows[-1]["id"] if rows else after,
                "has_more": len(rows) == limit}

    def artifacts(self, principal: Principal, tid: str, *, kind: str | None = None, limit: int = 50,
                  max_chars: int = 20000) -> dict:
        t = self._task(principal, tid)
        rows = [a for a in self.forge.artifacts.list(task_id=t["id"], kind=kind) if a["kind"] in PUBLIC_ARTIFACT_KINDS]
        rows = rows[-max(1, min(limit, 200)):]
        items = []
        for a in rows:
            item = {"id": a["id"], "kind": a["kind"], "title": self._s(a["title"], t, 300),
                    "created_at": iso(a["created_at"]), "content_type": a["content_type"]}
            if a.get("data") is not None:
                item["data"] = _sanitize_json(a["data"], lambda s: self._s(s, t, max_chars))
            else:
                item["content"] = self._s(a["content"], t, max_chars)
            items.append(item)
        return {"items": items, "total": len(items)}

    def agents(self, principal: Principal) -> dict:
        reg = self.forge.registry
        providers = []
        for st in reg.statuses():
            providers.append({
                "provider": st["provider"], "name": st.get("display_name"), "installed": st.get("installed"),
                "auth_state": st.get("auth_state"), "billing": st.get("billing"), "version": st.get("version"),
                "is_mock": st.get("is_mock", False),
                "models": [{"id": m["model_id"], "status": m["status"], "resolved_id": m["resolved_id"],
                            "tier": m["profile"].get("tier"), "enabled": m["enabled"]} for m in st.get("models", [])]})
        running = []
        for r in self.forge.db.query("SELECT r.task_id, r.phase, r.role, r.provider, r.model, r.started_at, t.project_id "
                                     "FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.state='running'"):
            if principal.can_access(r["project_id"]):
                running.append({"task_id": r["task_id"], "phase": r["phase"], "role": r["role"],
                                "provider": r["provider"], "model": r["model"], "started_at": iso(r["started_at"])})
        lock = self.forge.engine.lock_info() if self.forge.engine else None
        return {"providers": providers, "running": running, "engine_running": bool(lock and lock.get("alive"))}

    # ------------------------------------------------------------- writes
    def submit(self, principal: Principal, req: ExternalTaskRequest, *, source: str, source_ref: str | None = None,
               require_authorization: bool | None = None) -> dict:
        principal.require("tasks:write")
        p = self._project(principal, req.project)
        cfg = self.settings()
        workflow = req.workflow or p["config"]["default_workflow"]
        if workflow not in cfg.allowed_workflows:
            raise NotAllowed(f"workflow {workflow!r} is not allowed for external submissions")
        if p["status"] != "active":
            raise Conflict(f"project {p['name']} is {p['status']}")
        needs_auth = cfg.require_authorization if require_authorization is None else require_authorization
        meta = {"source": source, "source_ref": source_ref, "submitted_by": principal.name,
                "external": True, **({"collaboration": req.collaboration} if req.collaboration else {})}
        try:
            t = submit_task(
                self.forge, p["id"], title=clean_inbound(req.title, 200), objective=clean_inbound(req.objective, 8000),
                workflow=workflow, acceptance_criteria=[clean_inbound(c, 500) for c in req.acceptance_criteria],
                complexity=req.complexity, routing_mode=req.routing_mode, review_depth=req.review_depth,
                approval_policy=req.approval_policy, execution_mode="read_only" if req.read_only else None,
                metadata=meta, start=not needs_auth)
        except PermissionError as e:
            raise NotAllowed(str(e))
        except ValueError as e:   # includes collaboration.PolicyError subclasses raised as Exception below
            raise CoordinationError(str(e))
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PolicyError":
                raise CoordinationError(str(e))
            raise
        except KeyError as e:
            raise CoordinationError(str(e))
        if needs_auth:
            self.forge.approvals.request(
                p["id"], t["id"], "external_task", f"Authorize external task: {t['title'][:120]}",
                f"Submitted by {principal.name} via {source}" + (f" ({source_ref})" if source_ref else "")
                + f". Workflow {workflow}. Approve to queue it; reject to discard it.",
                {"source": source, "source_ref": source_ref})
            self.forge.tasks.transition(t["id"], TaskState.AWAITING_APPROVAL, f"awaiting authorization ({source})")
        self.forge.bus.emit("coordination.task_submitted", f"External task submitted via {source} by {principal.name}"
                            + (" (awaiting authorization)" if needs_auth else " (queued)"),
                            project_id=p["id"], task_id=t["id"],
                            data={"source": source, "principal": principal.name, "awaiting_authorization": needs_auth})
        return self.task_view(self.forge.tasks.get(t["id"]), detail=True)

    def feedback(self, principal: Principal, tid: str, fb: Feedback, *, source: str) -> dict:
        principal.require("feedback:write")
        t = self._task(principal, tid)
        msg = clean_inbound(fb.message, 4000)
        self.forge.artifacts.add(t["project_id"], t["id"], "external_feedback",
                                 f"Feedback from {principal.name} via {source}",
                                 {"kind": fb.kind, "message": msg, "from": principal.name, "source": source})
        self.forge.memory.add(t["project_id"], "note", f"External feedback ({fb.kind}) on {t['title'][:80]}",
                              msg[:2000], source=f"{source}:{principal.name}", source_task_id=t["id"], importance=0.6)
        result: dict[str, Any] = {"task_id": t["id"], "kind": fb.kind, "recorded": True}
        if fb.kind == "revision":
            if t["state"] != TaskState.AWAITING_APPROVAL.value or not any(
                    a["kind"] in ("merge", "review_unresolved")
                    for a in self.forge.approvals.list(task_id=t["id"], status="pending")):
                raise Conflict(f"revision requests apply only when the task awaits merge/review approval "
                               f"(current state {t['state']}); feedback was recorded")
            self.forge.engine.request_revision(t["id"], f"[external feedback from {principal.name} via {source}] {msg}")
            result["action"] = "revision queued: fix phase will address the latest review and this feedback"
        else:
            result["action"] = "recorded as feedback; agents receive it as a handoff artifact in later phases"
        self.forge.bus.emit("coordination.feedback", f"External {fb.kind} feedback via {source} by {principal.name}",
                            project_id=t["project_id"], task_id=t["id"], data={"kind": fb.kind})
        result["task"] = self.task_view(self.forge.tasks.get(t["id"]))
        return result


def _sanitize_json(v: Any, fn) -> Any:
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_sanitize_json(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _sanitize_json(x, fn) for k, x in v.items()}
    return v
