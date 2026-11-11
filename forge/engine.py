"""Persistent orchestration engine.

Guarantees:
* One active engine per FORGE_HOME (engine_lock row with heartbeat). A second
  process (e.g. the CLI while the server runs) only enqueues work.
* A task is driven by at most one coroutine (lease on the task row).
* A phase's completion (run result + phase pointer advance) is recorded in one
  transaction, so recovery re-runs only the phase that was interrupted.
* Crash recovery kills orphaned agent processes, marks their runs interrupted,
  saves and resets partial worktree changes, and requeues the task.
* Revision loops are bounded by `max_review_cycles`; retries by `max_retries`.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import gitops, integrations, prompts, schemas
from . import workflows as wfmod
from .db import dumps, loads, new_id, now
from .domain import ACTIVE, TERMINAL, InvalidTransition, ProjectConfig, TaskState
from .providers.base import ExecRequest, ExecResult
from .providers.proc import clean_env, kill_pid_group, pid_alive, run_streaming
from .router import RouteContext, RoutingError
from .workflows import Phase, Workflow

if TYPE_CHECKING:
    from .app import Forge

S = TaskState
LEASE_S = 60
LOCK_STALE_S = 20
FINISH = "__finish__"
AWAIT_CHILDREN = "__await_children__"
BLOCKING_SEVERITIES = {"blocker", "major"}
MAX_USER_REVISIONS = 3
SCHEMA_ARTIFACT_KIND = {"plan": "plan", "handoff": "handoff", "review": "review", "report": "report",
                        "proposal": "proposal"}


class Stop(Exception):
    """Internal: stop driving the task (state already persisted)."""


def submit_task(forge: "Forge", project_id: str, *, title: str, objective: str, workflow: str | None = None,
                **kw: Any) -> dict:
    """Validated task creation used by both API and CLI."""
    p = forge.projects.get(project_id)
    cfg = ProjectConfig.from_raw(p["config"])
    wf = wfmod.get(workflow or cfg.default_workflow)
    read_only = kw.get("execution_mode") == "read_only" or cfg.permissions.mode == "read_only"
    if read_only:
        kw["execution_mode"] = "read_only"
        writing = [ph.key for ph in wf.phases if ph.writes]
        if writing:
            raise PermissionError(f"workflow {wf.key!r} writes files (phases {writing}) but the project/task is "
                                  "read-only; use 'independent_proposals' or 'analyze'")
    if p["status"] != "active":
        raise PermissionError(f"project {p['name']} is {p['status']}; resume it first")
    milestone = kw.pop("as_milestone", False)
    t = forge.tasks.create(p["id"], title=title, objective=objective, workflow=wf.key,
                           category=kw.pop("category", wf.category), **kw)
    if milestone:
        forge.projects.add_milestone(p["id"], title, objective[:500], task_id=t["id"])
    return t


class Engine:
    def __init__(self, forge: "Forge"):
        self.forge = forge
        self.id = new_id("eng")
        self.running: dict[str, asyncio.Task] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.is_owner = False
        self._stop = asyncio.Event()
        self._shutting_down = False
        self.started_at = time.time()

    # ------------------------------------------------------------------ lock
    def acquire_lock(self) -> bool:
        t = now()
        with self.forge.db.tx() as c:
            row = c.execute("SELECT * FROM engine_lock WHERE id=1").fetchone()
            if row is None:
                c.execute("INSERT INTO engine_lock(id, owner, pid, heartbeat) VALUES (1,?,?,?)", (self.id, os.getpid(), t))
                return True
            if row["owner"] == self.id:
                c.execute("UPDATE engine_lock SET heartbeat=? WHERE id=1", (t,))
                return True
            stale = t - row["heartbeat"] > LOCK_STALE_S or (row["pid"] != os.getpid() and not pid_alive(row["pid"]))
            if stale:
                c.execute("UPDATE engine_lock SET owner=?, pid=?, heartbeat=? WHERE id=1", (self.id, os.getpid(), t))
                return True
            return False

    def release_lock(self) -> None:
        self.forge.db.execute("DELETE FROM engine_lock WHERE id=1 AND owner=?", (self.id,))
        self.is_owner = False

    def lock_info(self) -> dict | None:
        r = self.forge.db.one("SELECT * FROM engine_lock WHERE id=1")
        if r:
            r["alive"] = (now() - r["heartbeat"] < LOCK_STALE_S)
            r["is_self"] = r["owner"] == self.id
        return r

    # ------------------------------------------------------------------ loop
    async def run_forever(self, poll: float = 1.0) -> None:
        self.forge.bus.bind_loop(asyncio.get_running_loop())
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.step()
            except Exception as e:  # noqa: BLE001 - engine must keep running
                self.forge.bus.emit("engine.error", f"engine tick error: {e!r}", level="error")
            try:
                await asyncio.wait_for(self._stop.wait(), poll)
            except asyncio.TimeoutError:
                pass
        await self.shutdown()

    async def step(self) -> None:
        if not self.is_owner:
            if self.acquire_lock():
                self.is_owner = True
                self.forge.bus.emit("engine.started", f"Engine {self.id} acquired the execution lock (pid {os.getpid()})")
                self.recover()
            else:
                return
        else:
            if not self.acquire_lock():
                self.is_owner = False
                self.forge.bus.emit("engine.lost_lock", "Engine lost the execution lock", level="warn")
                return
        await self.tick()

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self._shutting_down = True
        for ev in self.cancel_events.values():
            ev.set()
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)
        self.release_lock()

    async def run_until_idle(self, task_ids: list[str] | None = None, timeout: float = 600, poll: float = 0.2) -> None:
        """Drive the engine in the foreground until the given tasks (or all) stop being runnable."""
        self.forge.bus.bind_loop(asyncio.get_running_loop())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.step()
            if not self.is_owner:
                raise RuntimeError("another Forge engine is running; submit work to it instead")
            if not self.running and not self._runnable(task_ids):
                return
            await asyncio.sleep(poll)
        raise TimeoutError("run_until_idle timed out")

    def _runnable(self, task_ids: list[str] | None) -> bool:
        sql = "SELECT id, state, phase FROM tasks WHERE state IN ('READY','PLANNING','EXECUTING','TESTING','REVIEWING','BLOCKED')"
        rows = self.forge.db.query(sql)
        for r in rows:
            if task_ids and r["id"] not in task_ids:
                continue
            if r["state"] == "BLOCKED" and r["phase"] != AWAIT_CHILDREN:
                continue
            if r["state"] == "READY":
                ok, failed = self.forge.tasks.deps_satisfied(r["id"])
                if not ok and not failed:
                    # waiting on another task: runnable only if that task can progress
                    continue
            return True
        return False

    async def tick(self) -> None:
        db = self.forge.db
        # 1. propagate control flags written by other processes (CLI) to running drivers
        for tid in list(self.running):
            r = db.one("SELECT pause_requested, cancel_requested FROM tasks WHERE id=?", (tid,))
            if r and (r["pause_requested"] or r["cancel_requested"]):
                self.cancel_events[tid].set()
            db.execute("UPDATE tasks SET lease_expires=? WHERE id=? AND lease_owner=?", (now() + LEASE_S, tid, self.id))
        # 2. parents waiting on decomposed subtasks
        for t in db.query("SELECT id FROM tasks WHERE state='BLOCKED' AND phase=?", (AWAIT_CHILDREN,)):
            self._check_children(t["id"])
        # 3. schedule ready work
        settings = self.forge.settings()
        if len(self.running) >= settings.max_global_concurrency:
            return
        rows = db.query(
            "SELECT t.id, t.project_id FROM tasks t JOIN projects p ON p.id=t.project_id "
            "WHERE t.state='READY' AND p.status='active' ORDER BY t.created_at ASC")
        for r in rows:
            if len(self.running) >= settings.max_global_concurrency:
                break
            if r["id"] in self.running:
                continue
            cfg = self.forge.projects.config(r["project_id"])
            in_project = sum(1 for tid in self.running
                             if (db.one("SELECT project_id FROM tasks WHERE id=?", (tid,)) or {}).get("project_id") == r["project_id"])
            if in_project >= cfg.limits.max_concurrency:
                continue
            ok, failed = self.forge.tasks.deps_satisfied(r["id"])
            if failed:
                self.forge.tasks.transition(r["id"], S.BLOCKED, f"dependency failed or cancelled: {', '.join(failed)}",
                                            error="dependency failed")
                continue
            if not ok:
                continue
            claimed = db.execute(
                "UPDATE tasks SET lease_owner=?, lease_expires=? WHERE id=? AND state='READY' "
                "AND (lease_owner IS NULL OR lease_expires < ? OR lease_owner=?)",
                (self.id, now() + LEASE_S, r["id"], now(), self.id))
            if claimed != 1:
                continue
            self.cancel_events[r["id"]] = asyncio.Event()
            self.running[r["id"]] = asyncio.create_task(self._drive_guarded(r["id"]))

    # ------------------------------------------------------------------ recovery
    def recover(self) -> dict:
        db = self.forge.db
        summary = {"runs_interrupted": 0, "processes_killed": 0, "tasks_requeued": 0, "tasks_failed": 0}
        for run in db.query("SELECT * FROM runs WHERE state='running' AND (engine_id IS NULL OR engine_id != ?)", (self.id,)):
            if run["pid"] and pid_alive(run["pid"]) and self._is_agent_process(run["pid"]):
                kill_pid_group(run["pid"])
                summary["processes_killed"] += 1
            db.execute("UPDATE runs SET state='interrupted', ended_at=?, error=? WHERE id=?",
                       (now(), "interrupted: engine stopped before the run finished", run["id"]))
            summary["runs_interrupted"] += 1
        placeholders = ",".join("?" * len(ACTIVE))
        for t in db.query(f"SELECT * FROM tasks WHERE state IN ({placeholders})", [s.value for s in ACTIVE]):
            if t["lease_owner"] == self.id:
                continue
            task = self.forge.tasks.get(t["id"])
            self._save_and_reset_partial(task, "interrupted")
            if task["cancel_requested"]:
                self.forge.tasks.transition(task["id"], S.CANCELLED, "cancel requested before crash",
                                            lease_owner=None, disposition="cancelled by user")
                continue
            if task["pause_requested"]:
                self.forge.tasks.transition(task["id"], S.PAUSED, "pause requested before crash", lease_owner=None,
                                            resume_state="READY", pause_requested=0)
                continue
            meta = task["metadata"]
            meta["interruptions"] = int(meta.get("interruptions", 0)) + 1
            limits = task["budget"]
            if meta["interruptions"] > int(limits.get("max_retries", 1)) + 2:
                self.forge.tasks.transition(task["id"], S.FAILED, "interrupted too many times", lease_owner=None,
                                            metadata=meta, error="repeated interruptions")
                summary["tasks_failed"] += 1
            else:
                self.forge.tasks.transition(task["id"], S.READY, f"recovered after interruption in phase {task['phase']}",
                                            lease_owner=None, lease_expires=None, metadata=meta)
                summary["tasks_requeued"] += 1
        db.execute("UPDATE tasks SET lease_owner=NULL, lease_expires=NULL WHERE lease_owner IS NOT NULL AND lease_owner != ?",
                   (self.id,))
        if any(summary.values()):
            self.forge.bus.emit("engine.recovered", "Recovery: " + ", ".join(f"{k}={v}" for k, v in summary.items()),
                                level="warn", data=summary)
        return summary

    @staticmethod
    def _is_agent_process(pid: int) -> bool:
        """Only kill a recorded pid if it still looks like an agent CLI (guards against pid reuse)."""
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=5).stdout
        except Exception:  # noqa: BLE001
            return False
        return bool(re.search(r"claude|codex|pytest|python|node", cmd))

    def _save_and_reset_partial(self, task: dict, why: str) -> None:
        wt = task.get("worktree_path")
        if not wt or not Path(wt).is_dir():
            return
        try:
            gitops.git(wt, "add", "-A")
            partial = gitops.git(wt, "diff", "--cached", "--no-color", check=False)
            if partial.strip():
                self.forge.artifacts.add(task["project_id"], task["id"], "diff",
                                         f"Partial changes discarded ({why}) in phase {task['phase']}", partial)
            gitops.git(wt, "reset", "-q", "--hard", "HEAD")
            gitops.git(wt, "clean", "-qfd")
        except gitops.GitError as e:
            self.forge.bus.emit("worktree.error", f"could not reset worktree: {e}", task_id=task["id"],
                                project_id=task["project_id"], level="error")

    # ------------------------------------------------------------------ driving
    async def _drive_guarded(self, tid: str) -> None:
        try:
            await self.drive(tid)
        except Stop:
            pass
        except Exception as e:  # noqa: BLE001
            t = self.forge.tasks.get(tid)
            self.forge.bus.emit("task.error", f"engine error: {e!r}", task_id=tid, project_id=t["project_id"],
                                level="error")
            if TaskState(t["state"]) not in TERMINAL:
                try:
                    self.forge.tasks.transition(tid, S.FAILED, f"engine error: {e}", error=str(e)[:2000])
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.forge.db.execute("UPDATE tasks SET lease_owner=NULL, lease_expires=NULL WHERE id=? AND lease_owner=?",
                                  (tid, self.id))
            self.running.pop(tid, None)
            self.cancel_events.pop(tid, None)

    async def drive(self, tid: str) -> None:
        task = self.forge.tasks.get(tid)
        wf = wfmod.get(task["workflow"])
        self._ensure_workspace(task)
        while True:
            task = self.forge.tasks.get(tid)
            st = TaskState(task["state"])
            if st not in (S.READY, *ACTIVE):
                return
            self._check_controls(task)
            project = self.forge.projects.get(task["project_id"])
            if project["status"] != "active":
                self._to(task, S.READY, "project paused; task released")
                return
            phase_key = task["phase"] or wf.phases[0].key
            if phase_key == FINISH:
                self._finish(task, wf)
                return
            phase = wf.phase(phase_key)
            self._check_task_runtime(task)
            if task["approval_policy"] == "every_phase" and phase.kind != "compare":
                if not self._phase_approved(task, phase):
                    return
            nxt = await self._run_phase(task, wf, phase)
            if nxt is None:
                return
            if nxt != phase.key:  # same key = retry of this phase; keep the attempt counter
                self.forge.tasks.update_fields(tid, phase=nxt, attempts=0)

    def _to(self, task: dict, state: TaskState, reason: str, **fields: Any) -> dict:
        return self.forge.tasks.transition(task["id"], state, reason, **fields)

    def _check_controls(self, task: dict) -> None:
        if task["cancel_requested"]:
            self._save_and_reset_partial(task, "cancelled")
            self._to(task, S.CANCELLED, "cancelled by user", disposition="cancelled by user", cancel_requested=0)
            raise Stop
        if task["pause_requested"]:
            self._save_and_reset_partial(task, "paused")
            self._to(task, S.PAUSED, "paused by user", resume_state="READY", pause_requested=0)
            raise Stop

    def _check_task_runtime(self, task: dict) -> None:
        lim = int(task["budget"].get("max_task_runtime_s", 7200))
        if task["started_at"] and now() - task["started_at"] > lim:
            self._to(task, S.FAILED, f"task runtime limit {lim}s exceeded", error="task runtime limit exceeded",
                     disposition="stopped by runtime limit")
            raise Stop

    def _phase_approved(self, task: dict, phase: Phase) -> bool:
        approved = task["metadata"].get("approved_phases", [])
        key = f"{phase.key}:{task['review_cycle']}"
        if key in approved:
            return True
        self.forge.approvals.request(task["project_id"], task["id"], "phase", f"Run phase '{phase.key}'",
                                     f"Approval policy 'every_phase' requires approval before {phase.key}.",
                                     {"phase_key": key})
        self._to(task, S.AWAITING_APPROVAL, f"waiting for approval to run {phase.key}")
        return False

    def _ensure_workspace(self, task: dict) -> None:
        if task["execution_mode"] == "read_only":
            return
        if task["worktree_path"] and Path(task["worktree_path"]).is_dir():
            return
        project = self.forge.projects.get(task["project_id"])
        root = Path(project["root_path"])
        cfg = ProjectConfig.from_raw(project["config"])
        dirty = gitops.dirty_files(root)
        wt = gitops.create_worktree(root, self.forge.paths.worktrees / project["id"], task["id"],
                                    cfg.base_branch or "HEAD")
        self.forge.tasks.update_fields(task["id"], worktree_path=str(wt.path), branch=wt.branch, base_commit=wt.base_commit)
        msg = f"Isolated worktree {wt.path} on branch {wt.branch} from {wt.base_commit[:10]}"
        if dirty:
            msg += f" (note: {len(dirty)} uncommitted change(s) in your working tree are not included and were not touched)"
        self.forge.bus.emit("worktree.created", msg, project_id=project["id"], task_id=task["id"],
                            data={"path": str(wt.path), "branch": wt.branch, "base": wt.base_commit})

    def _cwd(self, task: dict) -> Path:
        if task["execution_mode"] == "read_only":
            return Path(self.forge.projects.get(task["project_id"])["root_path"])
        return Path(task["worktree_path"])

    # ------------------------------------------------------------------ phases
    async def _run_phase(self, task: dict, wf: Workflow, phase: Phase) -> str | None:
        if TaskState(task["state"]) != phase.state:
            task = self._to(task, phase.state, f"phase {phase.key}", phase=phase.key)
        if phase.kind == "test":
            return await self._phase_test(task, wf, phase)
        if phase.kind == "fanout":
            return await self._phase_fanout(task, wf, phase)
        if phase.kind == "compare":
            self._phase_compare(task)
            return self._advance(task, wf, phase)
        res, run_id, decision = await self._agent_run(task, wf, phase)
        if res is None:
            return None
        if not res.ok:
            return self._handle_failure(task, phase, res)
        return self._after_success(task, wf, phase, res, run_id, decision)

    def _advance(self, task: dict, wf: Workflow, phase: Phase) -> str:
        keys = [p.key for p in wf.phases]
        if phase.key == "fix":
            # after a fix, re-test (if the workflow tests) and re-review
            tests = [p.key for p in wf.phases if p.kind == "test"]
            return tests[0] if tests else (wf.review_phase or FINISH)
        i = keys.index(phase.key)
        return keys[i + 1] if i + 1 < len(keys) else FINISH

    def _route(self, task: dict, phase: Phase, *, avoid: list[str] | None = None,
               exclude: list[tuple[str, str]] | None = None, override: dict | None = None) -> Any:
        overrides = task["routing_overrides"] or {}
        ov = override or overrides.get(phase.key)
        if ov is None and phase.key == "fix":
            impl = next((p.key for p in wfmod.get(task["workflow"]).phases if p.writes), None)
            ov = overrides.get(impl) if impl else None
        if isinstance(ov, list):
            ov = None
        if ov is None and overrides.get("*"):
            ov = overrides["*"]
        mode = task["routing_mode"]
        escalation = 0
        if mode == "economy":
            escalation = int(task["attempts"])
        ctx = RouteContext(project_id=task["project_id"], task_id=task["id"], phase=phase.key, role=phase.role,
                           category=task["category"], complexity=task["complexity"], criticality=task["criticality"],
                           mode=mode, override=ov, avoid_providers=avoid or [],
                           independent=task["review_depth"] == "independent" or mode == "max_quality",
                           escalation=escalation, exclude=exclude or [])
        return self.forge.router.route(ctx)

    def _writer_providers(self, task: dict) -> list[str]:
        rows = self.forge.db.query(
            "SELECT DISTINCT provider FROM runs WHERE task_id=? AND role IN ('implementer','fixer','builder','experimenter') "
            "AND state='succeeded'", (task["id"],))
        return [r["provider"] for r in rows if r["provider"]]

    def _known_spend(self, project_id: str) -> float:
        total = 0.0
        for r in self.forge.db.query("SELECT usage FROM runs WHERE project_id=?", (project_id,)):
            u = loads(r["usage"], {})
            if u.get("cost_kind") == "billed" and u.get("cost_usd"):
                total += float(u["cost_usd"])
        return total

    def _handoffs(self, task: dict, phase: Phase) -> list[dict]:
        arts = self.forge.artifacts.list(task_id=task["id"])
        latest: dict[str, dict] = {}
        for a in arts:
            latest[a["kind"]] = a
        want: list[str]
        if phase.role in ("implementer", "builder", "experimenter"):
            want = ["plan", "report"]
        elif phase.role == "fixer":
            want = ["plan", "review", "test_report", "handoff", "revision_request"]
        elif phase.role == "reviewer":
            want = ["plan", "handoff", "test_report"]
        elif phase.role in ("analyst", "researcher"):
            want = ["report", "handoff", "test_report"]
        else:
            want = []
        out = [latest[k] for k in want if k in latest]
        if phase.role == "reviewer" and task["base_commit"] and task["worktree_path"]:
            d = gitops.diff(task["worktree_path"], task["base_commit"], "HEAD")
            out.append({"kind": "diff", "title": f"Cumulative diff {task['base_commit'][:10]}..HEAD",
                        "content": d or "(no changes)"})
        if phase.role in ("researcher",) and phase.key == "hypothesis":
            out = [a for a in arts if a["kind"] == "report"][-2:]
        return out

    def _shell_allowlist(self, cfg: ProjectConfig) -> list[str]:
        allow = ["python -m pytest", "python3 -m pytest", "pytest", "git status", "git diff", "git log", "ls"]
        if cfg.test_command:
            parts = shlex.split(cfg.test_command.replace("{python}", sys.executable))
            if parts:
                allow.insert(0, " ".join(parts[:3]))
        return allow

    async def _agent_run(self, task: dict, wf: Workflow, phase: Phase, *, decision: Any = None,
                         emit_suffix: str = "") -> tuple[ExecResult | None, str | None, Any]:
        cfg = self.forge.projects.config(task["project_id"])
        limits = task["budget"]
        spent = self._known_spend(task["project_id"])
        if spent > float(limits.get("max_known_api_usd", 0)) or (spent > 0 and not cfg.permissions.allow_paid_api):
            self.forge.approvals.request(task["project_id"], task["id"], "spending",
                                         "Known API spending limit reached",
                                         f"Recorded billed spend ${spent:.2f} exceeds the limit "
                                         f"${float(limits.get('max_known_api_usd', 0)):.2f}.", {"spent": spent})
            self._to(task, S.BLOCKED, "known API spending limit reached", error="spending limit")
            return None, None, None
        if decision is None:
            try:
                avoid = self._writer_providers(task) if phase.role == "reviewer" else []
                decision = self._route(task, phase, avoid=avoid)
            except RoutingError as e:
                self._to(task, S.BLOCKED, f"routing failed: {e}", error=str(e)[:2000])
                self.forge.memory.add(task["project_id"], "problem", f"Routing failed for {phase.key}", str(e),
                                      source_task_id=task["id"], importance=0.6)
                return None, None, None
        adapter = self.forge.registry.get(decision.provider)
        read_only = task["execution_mode"] == "read_only" or not phase.writes
        schema = schemas.BY_NAME.get(phase.schema) if phase.schema else None
        run_id = new_id("r")
        log_dir = self.forge.paths.logs / task["id"]
        log_dir.mkdir(parents=True, exist_ok=True)
        memory = self.forge.memory.render(self.forge.memory.retrieve(
            task["project_id"], f"{task['title']} {task['objective']} {phase.key}", task_id=task["id"],
            include_shared=cfg.share_memory))
        extra = ""
        if phase.role == "fixer" and task["metadata"].get("fix_all_issues"):
            extra = ("The user requested a revision: address every issue in the latest review (all severities) and "
                     "the user's note in the revision request, plus any failing tests. Remove unwanted files you find.")
        elif phase.role == "fixer":
            extra = ("Address only confirmed issues of severity blocker/major from the latest review, and any failing "
                     "tests in the latest test report.")
        prompt = prompts.build(phase, task, instructions=self.forge.projects.trusted_instructions(task["project_id"]),
                               memory=memory, handoffs=self._handoffs(task, phase), read_only=read_only,
                               test_command=cfg.test_command.replace("{python}", sys.executable),
                               schema=schema, extra=extra)
        remaining = int(limits.get("max_task_runtime_s", 7200)) - int(now() - (task["started_at"] or now()))
        timeout = max(1, min(int(limits.get("max_runtime_s", 1800)), remaining))
        attempt = int(task["attempts"]) + 1
        with self.forge.db.tx() as c:
            c.execute(
                "INSERT INTO runs(id,task_id,project_id,phase,role,kind,provider,model,effort,state,attempt,review_cycle,"
                "prompt,log_path,engine_id,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task["id"], task["project_id"], phase.key, phase.role, "agent", decision.provider,
                 decision.model, decision.effort, "running", attempt, task["review_cycle"], prompt,
                 str(log_dir / f"{run_id}.log"), self.id, now()))
            c.execute("UPDATE tasks SET assigned_provider=?, assigned_model=? WHERE id=?",
                      (decision.provider, decision.model, task["id"]))
        self.forge.router.attach_run(decision.id, run_id)
        mock = " [MOCK]" if adapter.is_mock else ""
        self.forge.bus.emit("run.started", f"{phase.key}{emit_suffix}: {phase.role} → {decision.provider}/{decision.model}"
                            + (f" (effort {decision.effort})" if decision.effort else "") + mock,
                            project_id=task["project_id"], task_id=task["id"], run_id=run_id,
                            data={"provider": decision.provider, "model": decision.model, "phase": phase.key,
                                  "mock": adapter.is_mock})

        async def on_event(typ: str, msg: str, data: dict) -> None:
            self.forge.bus.emit(typ, msg, project_id=task["project_id"], task_id=task["id"], run_id=run_id,
                                level="error" if typ == "agent.error" else "info", data=data)

        def on_start(pid: int) -> None:
            self.forge.db.execute("UPDATE runs SET pid=? WHERE id=?", (pid, run_id))

        req = ExecRequest(
            prompt=prompt, cwd=self._cwd(task), role=phase.role, model=decision.model, effort=decision.effort,
            sandbox="read_only" if read_only else "workspace_write",
            allow_shell=(not read_only) and cfg.permissions.allow_agent_shell,
            shell_allowlist=self._shell_allowlist(cfg), allow_network=cfg.permissions.allow_network_tools,
            output_schema=schema, system_append=prompts.system_prompt(phase, bool(schema)), timeout_s=timeout,
            max_turns=int(limits.get("max_turns", 40)), log_path=log_dir / f"{run_id}.log",
            mcp_config_path=integrations.mcp_config_for(self.forge, task["project_id"]),
            metadata={"mock": task["metadata"].get("mock"), "attempt": attempt, "review_cycle": task["review_cycle"],
                      "objective": task["objective"], "acceptance_criteria": task["acceptance_criteria"]},
        )
        cancel = self.cancel_events.setdefault(task["id"], asyncio.Event())
        try:
            res = await adapter.execute(req, on_event, cancel, on_start)
        except Exception as e:  # noqa: BLE001
            res = ExecResult(ok=False, error=f"adapter error: {e!r}", error_kind="other")
        if res.ok and task["metadata"].get("mock", {}).get("billed_cost_usd"):
            res.usage.cost_usd = float(task["metadata"]["mock"]["billed_cost_usd"])
            res.usage.cost_kind, res.usage.billing = "billed", "api"
        state = "succeeded" if res.ok else ("cancelled" if res.error_kind == "cancelled" else "failed")
        if self._shutting_down and res.error_kind == "cancelled":
            state = "interrupted"
        self.forge.db.execute(
            "UPDATE runs SET state=?, output_text=?, structured_output=?, usage=?, session_id=?, resolved_model=?, "
            "exit_code=?, error=?, ended_at=? WHERE id=?",
            (state, (res.text or "")[:200000], dumps(res.structured) if res.structured is not None else None,
             dumps(res.usage.as_dict()), res.session_id, res.resolved_model, res.exit_code, res.error, now(), run_id))
        if not adapter.is_mock:
            self.forge.registry.record_run_outcome(decision.provider, decision.model, res.ok, res.error_kind,
                                                   res.resolved_model, res.error or "")
        self.forge.router.record_outcome(decision.id, {"ok": res.ok, "error_kind": res.error_kind,
                                                       "duration_s": round(res.duration_s, 1)})
        if res.permission_denials:
            self.forge.bus.emit("agent.permission_denied", f"{len(res.permission_denials)} tool call(s) denied by policy",
                                project_id=task["project_id"], task_id=task["id"], run_id=run_id, level="warn",
                                data={"denials": res.permission_denials[:20]})
        self.forge.bus.emit("run.finished", f"{phase.key}{emit_suffix}: {state}" + (f" — {res.error}" if res.error else "")
                            + f" ({res.duration_s:.1f}s)", project_id=task["project_id"], task_id=task["id"],
                            run_id=run_id, level="info" if res.ok else "warn",
                            data={"state": state, "usage": res.usage.as_dict(), "resolved_model": res.resolved_model})
        return res, run_id, decision

    def _after_success(self, task: dict, wf: Workflow, phase: Phase, res: ExecResult, run_id: str, decision: Any) -> str | None:
        pid, tid = task["project_id"], task["id"]
        label = f"{decision.provider}/{res.resolved_model or decision.model}"
        kind = SCHEMA_ARTIFACT_KIND.get(phase.schema or "", "note")
        content: Any = res.structured if res.structured is not None else (res.text or "(no output)")
        self.forge.artifacts.add(pid, tid, kind, f"{phase.key} ({phase.role}) by {label}", content, run_id=run_id)
        if phase.writes and task["execution_mode"] != "read_only":
            sha = gitops.commit_all(task["worktree_path"], f"forge({phase.key}): {task['title'][:60]} [{label}]")
            if sha:
                d = gitops.git(task["worktree_path"], "show", "--no-color", "--format=%H %s", sha)
                self.forge.artifacts.add(pid, tid, "diff", f"Changes from {phase.key} ({sha[:10]})", d, run_id=run_id)
                self.forge.bus.emit("worktree.commit", f"Committed {phase.key} changes as {sha[:10]}", project_id=pid,
                                    task_id=tid, run_id=run_id, data={"sha": sha})
            else:
                self.forge.bus.emit("worktree.no_changes", f"{phase.key} made no file changes", project_id=pid,
                                    task_id=tid, run_id=run_id, level="warn")
        s = res.structured or {}
        if phase.key == "fix" and task["metadata"].get("fix_all_issues"):
            self.forge.tasks.update_fields(tid, metadata={**task["metadata"], "fix_all_issues": False})
        if phase.schema == "plan":
            self.forge.memory.add(pid, "decision", f"Plan for: {task['title'][:120]}",
                                  s.get("summary", "") + "\nSteps: " + "; ".join(s.get("steps", [])[:12]),
                                  source_task_id=tid, source_run_id=run_id, importance=0.6)
            if task["metadata"].get("decompose") and len(s.get("subtasks") or []) >= 2:
                self._decompose(task, s["subtasks"])
                raise Stop
        if phase.schema == "report" and s.get("findings"):
            self.forge.memory.add(pid, "research", f"{phase.key}: {task['title'][:100]}",
                                  s.get("summary", "") + "\nFindings: " + "; ".join(s["findings"][:10]),
                                  source_task_id=tid, source_run_id=run_id, importance=0.55)
        if phase.key == wf.review_phase:
            return self._after_review(task, wf, phase, s, run_id)
        return self._advance(task, wf, phase)

    def _after_review(self, task: dict, wf: Workflow, phase: Phase, review: dict, run_id: str) -> str | None:
        blocking = [i for i in review.get("issues", []) if i.get("severity") in BLOCKING_SEVERITIES]
        max_cycles = int(task["budget"].get("max_review_cycles", 2))
        if review.get("verdict") == "approve" or not blocking:
            if review.get("issues"):
                self.forge.memory.add(task["project_id"], "note", f"Non-blocking review notes: {task['title'][:80]}",
                                      "; ".join(i.get("description", "") for i in review["issues"][:8]),
                                      source_task_id=task["id"], source_run_id=run_id, importance=0.3)
            return FINISH
        if task["review_cycle"] < max_cycles and wf.fix_phase:
            self.forge.tasks.update_fields(task["id"], review_cycle=task["review_cycle"] + 1)
            self.forge.bus.emit("review.changes_requested",
                                f"Review requested changes ({len(blocking)} blocking issue(s)); "
                                f"revision cycle {task['review_cycle'] + 1}/{max_cycles}",
                                project_id=task["project_id"], task_id=task["id"], run_id=run_id, level="warn")
            return "fix"
        detail = "\n".join(f"- [{i['severity']}] {i.get('file','')}: {i.get('description','')}" for i in blocking)
        self.forge.memory.add(task["project_id"], "problem", f"Unresolved review issues: {task['title'][:80]}", detail,
                              source_task_id=task["id"], source_run_id=run_id, importance=0.8)
        self.forge.approvals.request(task["project_id"], task["id"], "review_unresolved",
                                     f"Review issues unresolved after {max_cycles} cycle(s)",
                                     detail + "\n\nApprove to accept the changes anyway (merge will still need "
                                              "approval), or reject to fail the task.")
        self.forge.tasks.update_fields(task["id"], phase=FINISH)
        self._to(task, S.AWAITING_APPROVAL, "review cycles exhausted with unresolved issues")
        return None

    def _handle_failure(self, task: dict, phase: Phase, res: ExecResult) -> str | None:
        task = self.forge.tasks.get(task["id"])
        if res.error_kind == "cancelled":
            if self._shutting_down:
                self._save_and_reset_partial(task, "shutdown")
                self._to(task, S.READY, "engine shutting down; will resume", lease_owner=None)
                return None
            self._check_controls(task)  # raises Stop for pause/cancel
            return None
        self._save_and_reset_partial(task, f"failed {phase.key}")
        err = res.error or "unknown error"
        self.forge.memory.add(task["project_id"], "failure", f"{phase.key} failed ({res.error_kind})", err[:2000],
                              source_task_id=task["id"], importance=0.6)
        if res.error_kind in ("auth", "rate_limit", "model_unavailable"):
            self._to(task, S.BLOCKED, f"{phase.key}: {res.error_kind} — user action needed", error=err[:2000])
            return None
        attempts = int(task["attempts"]) + 1
        max_retries = int(task["budget"].get("max_retries", 1))
        if attempts <= max_retries:
            self.forge.tasks.update_fields(task["id"], attempts=attempts)
            self.forge.bus.emit("task.retry", f"Retrying {phase.key} (attempt {attempts + 1}/{max_retries + 1}): {err[:200]}",
                                project_id=task["project_id"], task_id=task["id"], level="warn")
            return phase.key
        self._to(task, S.FAILED, f"{phase.key} failed after {attempts} attempt(s): {err[:300]}", error=err[:2000],
                 disposition=f"failed in {phase.key}")
        return None

    # --- test phase ----------------------------------------------------------------
    async def _phase_test(self, task: dict, wf: Workflow, phase: Phase) -> str | None:
        cfg = self.forge.projects.config(task["project_id"])
        pid, tid = task["project_id"], task["id"]
        if not cfg.test_command:
            self.forge.artifacts.add(pid, tid, "test_report", "Tests: not configured",
                                     {"passed": None, "skipped": True, "summary": "No test command configured for "
                                      "this project; no automated test evidence."})
            self.forge.bus.emit("test.skipped", "No test command configured; skipping tests (no evidence recorded)",
                                project_id=pid, task_id=tid, level="warn")
            return self._advance(task, wf, phase)
        argv = shlex.split(cfg.test_command.replace("{python}", sys.executable))
        run_id = new_id("r")
        log_dir = self.forge.paths.logs / tid
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{run_id}.log"
        self.forge.db.execute(
            "INSERT INTO runs(id,task_id,project_id,phase,role,kind,state,attempt,review_cycle,prompt,log_path,engine_id,"
            "started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, tid, pid, phase.key, "tester", "test", "running", int(task["attempts"]) + 1, task["review_cycle"],
             cfg.test_command, str(log_path), self.id, now()))
        self.forge.bus.emit("test.started", f"Running tests: {cfg.test_command}", project_id=pid, task_id=tid,
                            run_id=run_id)
        lines: list[str] = []

        def on_line(line: str) -> None:
            lines.append(line)

        venv_bin = Path(sys.executable).parent
        env = clean_env(path_prepend=[str(venv_bin)])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cancel = self.cancel_events.setdefault(tid, asyncio.Event())
        try:
            out = await run_streaming(argv + [], cwd=self._cwd(task), env=env, stdin_text=None,
                                      timeout_s=int(task["budget"].get("test_timeout_s", 600)), cancel=cancel,
                                      on_line=on_line, log_path=log_path,
                                      on_start=lambda p: self.forge.db.execute("UPDATE runs SET pid=? WHERE id=?", (p, run_id)))
        except FileNotFoundError as e:
            self.forge.db.execute("UPDATE runs SET state='failed', error=?, ended_at=? WHERE id=?", (str(e), now(), run_id))
            self._to(task, S.BLOCKED, f"test command not found: {e}", error=str(e))
            return None
        output = "\n".join(lines) + ("\n" + out.stderr if out.stderr else "")
        if out.cancelled:
            self.forge.db.execute("UPDATE runs SET state=?, ended_at=? WHERE id=?",
                                  ("interrupted" if self._shutting_down else "cancelled", now(), run_id))
            return self._handle_failure(task, phase, ExecResult(ok=False, error="cancelled", error_kind="cancelled"))
        passed = out.exit_code == 0 and not out.timed_out
        summary = _test_summary(output) or (f"exit code {out.exit_code}" + (" (timed out)" if out.timed_out else ""))
        self.forge.db.execute(
            "UPDATE runs SET state=?, exit_code=?, output_text=?, ended_at=?, error=? WHERE id=?",
            ("succeeded" if passed else "failed", out.exit_code, output[-100000:], now(),
             None if passed else summary, run_id))
        self.forge.artifacts.add(pid, tid, "test_report", f"Tests {'passed' if passed else 'FAILED'}: {summary}",
                                 {"passed": passed, "exit_code": out.exit_code, "command": cfg.test_command,
                                  "summary": summary, "duration_s": round(out.duration_s, 2),
                                  "timed_out": out.timed_out, "output_tail": output[-6000:]}, run_id=run_id)
        self.forge.bus.emit("test.finished", f"Tests {'passed' if passed else 'failed'}: {summary}", project_id=pid,
                            task_id=tid, run_id=run_id, level="info" if passed else "warn",
                            data={"passed": passed, "exit_code": out.exit_code})
        self.forge.memory.add(pid, "test_result", f"Tests {'passed' if passed else 'failed'} ({phase.key}, cycle "
                              f"{task['review_cycle']})", f"{cfg.test_command}: {summary}", source_task_id=tid,
                              source_run_id=run_id, importance=0.3 if passed else 0.6)
        if passed:
            return self._advance(task, wf, phase)
        max_cycles = int(task["budget"].get("max_review_cycles", 2))
        if wf.fix_phase and task["review_cycle"] < max_cycles:
            self.forge.tasks.update_fields(tid, review_cycle=task["review_cycle"] + 1)
            self.forge.bus.emit("test.fix_loop", f"Tests failed; starting fix cycle {task['review_cycle'] + 1}/{max_cycles}",
                                project_id=pid, task_id=tid, level="warn")
            return "fix"
        self._to(task, S.FAILED, f"tests failing after {task['review_cycle']} fix cycle(s): {summary}",
                 error=summary, disposition="tests failing; changes kept on branch for inspection")
        return None

    # --- fan-out / compare -------------------------------------------------------------
    async def _phase_fanout(self, task: dict, wf: Workflow, phase: Phase) -> str | None:
        overrides = (task["routing_overrides"] or {}).get(phase.key)
        count = int(task["metadata"].get("fanout_count", 2))
        decisions = []
        try:
            if isinstance(overrides, list) and overrides:
                for ov in overrides:
                    decisions.append(self._route(task, phase, override=ov))
            else:
                used: list[tuple[str, str]] = []
                providers: list[str] = []
                for _ in range(count):
                    d = self._route(task, phase, avoid=providers, exclude=used)
                    decisions.append(d)
                    used.append((d.provider, d.model))
                    providers.append(d.provider)
        except RoutingError as e:
            if not decisions:
                self._to(task, S.BLOCKED, f"routing failed: {e}", error=str(e)[:2000])
                return None
            self.forge.bus.emit("routing.partial", f"Only {len(decisions)} proposer(s) available: {e}",
                                project_id=task["project_id"], task_id=task["id"], level="warn")
        results = await asyncio.gather(*[
            self._agent_run(task, wf, phase, decision=d, emit_suffix=f" #{i + 1}") for i, d in enumerate(decisions)])
        ok = 0
        for res, run_id, d in results:
            if res is not None and res.ok:
                ok += 1
                label = f"{d.provider}/{res.resolved_model or d.model}"
                self.forge.artifacts.add(task["project_id"], task["id"], "proposal", f"Proposal by {label}",
                                         res.structured or {"summary": res.text}, run_id=run_id)
        if ok == 0:
            first = next((r for r, _, _ in results if r is not None), None)
            return self._handle_failure(task, phase, first or ExecResult(ok=False, error="no proposer ran"))
        if ok < len(results):
            self.forge.bus.emit("fanout.partial", f"{ok}/{len(results)} proposals succeeded",
                                project_id=task["project_id"], task_id=task["id"], level="warn")
        return self._advance(task, wf, phase)

    def _phase_compare(self, task: dict) -> None:
        props = []
        for a in self.forge.artifacts.list(task_id=task["id"], kind="proposal"):
            run = self.forge.db.one("SELECT provider, model, resolved_model, effort FROM runs WHERE id=?", (a["run_id"],)) or {}
            props.append({"artifact_id": a["id"], "run_id": a["run_id"], **run, "proposal": a.get("data") or {}})
        root = Path(self.forge.projects.get(task["project_id"])["root_path"])
        comparison = compare_proposals(props, root)
        self.forge.artifacts.add(task["project_id"], task["id"], "comparison",
                                 f"Comparison of {len(props)} independent proposals", comparison)

    # --- finish / approvals ----------------------------------------------------------------
    def _finish(self, task: dict, wf: Workflow) -> None:
        pid, tid = task["project_id"], task["id"]
        cfg = self.forge.projects.config(pid)
        has_changes = False
        if task["execution_mode"] != "read_only" and task["base_commit"]:
            d = gitops.diff(task["worktree_path"], task["base_commit"], "HEAD")
            has_changes = bool(d.strip())
            if has_changes:
                stat = gitops.diff(task["worktree_path"], task["base_commit"], "HEAD", stat=True)
                self.forge.artifacts.add(pid, tid, "diff", f"Final diff {task['base_commit'][:10]}..{task['branch']}",
                                         d)
        if not has_changes:
            self._complete(task, "completed (no code changes)" if task["execution_mode"] != "read_only"
                           else "completed (read-only)")
            return
        flagged = gitops.ai_attribution_in_range(task["worktree_path"], task["base_commit"], "HEAD")
        attribution_note = ""
        if flagged:
            attribution_note = ("\nWarning: agent-made commits carry AI attribution lines (Forge's own commits never do): "
                                + "; ".join(f"{f['sha'][:10]}: {' | '.join(f['lines'])}" for f in flagged))
            self.forge.bus.emit("worktree.ai_attribution", attribution_note.strip(), project_id=pid, task_id=tid,
                                level="warn", data={"commits": flagged})
        if task["approval_policy"] == "never_merge":
            self._complete(task, f"completed; changes left on branch {task['branch']} (policy: never merge)")
            return
        if cfg.permissions.auto_merge and "merge" not in cfg.permissions.require_approval_for:
            self._merge(task)
            return
        self.forge.approvals.request(pid, tid, "merge", f"Merge {task['branch']} into your working branch",
                                     f"Diff summary:\n{stat}\nMerging modifies your project working tree "
                                     "(refused if it has uncommitted changes)." + attribution_note,
                                     {"branch": task["branch"], "base": task["base_commit"]})
        self._to(task, S.AWAITING_APPROVAL, "changes ready; waiting for merge approval", phase=FINISH)

    def _merge(self, task: dict) -> None:
        project = self.forge.projects.get(task["project_id"])
        try:
            sha = gitops.merge_branch(Path(project["root_path"]), task["branch"],
                                      f"Merge {task['branch']}: {task['title'][:60]} (Forge)")
        except gitops.GitError as e:
            self._to(task, S.BLOCKED, f"merge refused: {e}", error=str(e)[:1000])
            return
        self.forge.bus.emit("task.merged", f"Merged {task['branch']} as {sha[:10]}", project_id=task["project_id"],
                            task_id=task["id"])
        self._complete(task, f"merged into {gitops.current_branch(project['root_path'])} at {sha[:10]}")

    def _complete(self, task: dict, disposition: str) -> None:
        self._to(task, S.COMPLETED, disposition, disposition=disposition, phase=FINISH)
        for m in self.forge.db.query("SELECT id FROM milestones WHERE task_id=?", (task["id"],)):
            self.forge.projects.set_milestone_status(m["id"], "done")
        self.forge.memory.add(task["project_id"], "milestone", f"Completed: {task['title'][:150]}", disposition,
                              source_task_id=task["id"], importance=0.5)
        if task["parent_id"]:
            self._check_children(task["parent_id"])

    def apply_approval(self, approval_id: str, approve: bool, note: str = "") -> dict:
        """Called by API/CLI. Applies the decision's effect on the task."""
        a = self.forge.approvals.decide(approval_id, approve, note)
        if not a["task_id"]:
            return a
        task = self.forge.tasks.get(a["task_id"])
        self.forge.memory.add(task["project_id"], "user_decision",
                              f"{'Approved' if approve else 'Rejected'}: {a['title'][:150]}",
                              note or a["detail"][:1000], source_task_id=task["id"], importance=0.9)
        kind = a["kind"]
        if task["state"] != S.AWAITING_APPROVAL.value and kind not in ("spending",):
            return a
        if kind == "merge":
            if approve:
                self._merge(task)
            else:
                self._complete(task, f"not merged (rejected by user); changes kept on branch {task['branch']}")
        elif kind == "review_unresolved":
            if approve:
                self._to(task, S.READY, "user accepted unresolved review issues", phase=FINISH)
            else:
                self._to(task, S.FAILED, "user rejected unresolved review issues", disposition="rejected at review")
        elif kind == "phase":
            if approve:
                meta = task["metadata"]
                meta.setdefault("approved_phases", []).append(a["payload"]["phase_key"])
                self._to(task, S.READY, "phase approved", metadata=meta)
            else:
                self._to(task, S.CANCELLED, "phase rejected by user", disposition="phase rejected")
        elif kind == "spending" and approve and task["state"] == S.BLOCKED.value:
            self._to(task, S.READY, "spending limit acknowledged (limit itself unchanged)")
        return a

    def request_revision(self, task_id: str, note: str = "") -> dict:
        """User sends the latest review (all severities) plus a note back to the fix phase.

        Only from AWAITING_APPROVAL (merge / unresolved review). Bounded: at most
        MAX_USER_REVISIONS per task, on top of the automatic review cycles."""
        task = self.forge.tasks.get(task_id)
        if task["state"] != S.AWAITING_APPROVAL.value:
            raise InvalidTransition("revisions can only be requested while a task awaits approval")
        wf = wfmod.get(task["workflow"])
        if not wf.fix_phase:
            raise ValueError(f"workflow {wf.key} has no fix phase")
        meta = task["metadata"]
        used = int(meta.get("user_revisions", 0))
        if used >= MAX_USER_REVISIONS:
            raise ValueError(f"revision limit reached ({MAX_USER_REVISIONS} user-requested revisions)")
        for a in self.forge.approvals.list(task_id=task_id, status="pending"):
            self.forge.approvals.decide(a["id"], False, f"revision requested: {note}".strip())
        self.forge.artifacts.add(task["project_id"], task_id, "revision_request", "Revision requested by user",
                                 {"note": note, "instruction": "Address every issue in the latest review "
                                  "(all severities, including minor and nit) and the user's note."})
        self.forge.memory.add(task["project_id"], "user_decision", f"Revision requested: {task['title'][:120]}",
                              note or "address all review issues", source_task_id=task_id, importance=0.9)
        meta.update(user_revisions=used + 1, fix_all_issues=True)
        return self._to(task, S.READY, "revision requested by user", phase="fix", metadata=meta,
                        review_cycle=task["review_cycle"] + 1)

    # --- decomposition ---------------------------------------------------------------
    def _decompose(self, task: dict, subtasks: list[dict]) -> None:
        ids: list[str] = []
        for i, st in enumerate(subtasks[:12]):
            deps = [ids[j] for j in st.get("depends_on", []) if isinstance(j, int) and 0 <= j < len(ids)]
            child = self.forge.tasks.create(
                task["project_id"], title=st.get("title", f"Subtask {i + 1}")[:200],
                objective=st.get("objective", ""), workflow=task["workflow"], category=task["category"],
                depends_on=deps, parent_id=task["id"], complexity=task["complexity"], criticality=task["criticality"],
                routing_mode=task["routing_mode"], routing_overrides=task["routing_overrides"],
                review_depth=task["review_depth"], approval_policy=task["approval_policy"],
                execution_mode=task["execution_mode"], budget=task["budget"],
                metadata={k: v for k, v in task["metadata"].items() if k != "decompose"})
            self.forge.projects.add_milestone(task["project_id"], child["title"], child["objective"][:500],
                                              task_id=child["id"])
            ids.append(child["id"])
        self.forge.tasks.update_fields(task["id"], phase=AWAIT_CHILDREN, metadata={**task["metadata"], "children": ids})
        self._to(task, S.BLOCKED, f"decomposed into {len(ids)} subtasks; waiting for them")

    def _check_children(self, parent_id: str) -> None:
        parent = self.forge.tasks.get(parent_id)
        if parent["phase"] != AWAIT_CHILDREN or parent["state"] != S.BLOCKED.value:
            return
        kids = self.forge.tasks.list(parent_id=parent_id)
        if not kids or any(TaskState(k["state"]) not in TERMINAL for k in kids):
            return
        if all(k["state"] == S.COMPLETED.value for k in kids):
            self._complete(parent, f"all {len(kids)} subtasks completed")
        else:
            bad = [k["id"] for k in kids if k["state"] != S.COMPLETED.value]
            self._to(parent, S.FAILED, f"subtasks not completed: {', '.join(bad)}", disposition="subtasks failed")


_PYTEST = re.compile(r"^(?:=+ )?(\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b.*?|no tests ran) in [\d.]+s",
                     re.I | re.M)


def _test_summary(output: str) -> str:
    m = None
    for m in _PYTEST.finditer(output):
        pass
    if m:
        return m.group(1).strip()
    tail = [l for l in output.strip().splitlines() if l.strip()][-1:]  # noqa: E741
    return tail[0][:200] if tail else ""


_TOK = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "then", "than", "each", "any", "all", "are", "was",
    "were", "will", "would", "should", "must", "may", "can", "not", "but", "its", "their", "those", "these", "such",
    "use", "using", "used", "via", "per", "also", "only", "both", "before", "after", "when", "where", "which", "what",
    "how", "other", "more", "most", "less", "there", "here", "have", "has", "had", "been", "being", "does", "did",
    "record", "records", "including", "include", "includes", "between", "within", "without", "about", "over",
}
_PATH = re.compile(r"(?<![\w/])((?:[\w.-]+/)+(?:[\w.-]+\.(?:md|json|py|csv|bib|toml|txt|yaml|yml|ts|tsx))?|"
                   r"[\w.-]+\.(?:md|json|py|csv|bib|toml|txt|yaml|yml))")


def _stem(t: str) -> str:
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + rep
    return t


def _toks(s: str) -> set[str]:
    return {_stem(t) for t in _TOK.findall(s.lower().replace(".", " ")) if len(t) >= 3 and t not in _STOPWORDS}


def _sim(a: str, b: str) -> float:
    """Overlap coefficient of content words: robust to one text being much longer."""
    x, y = _toks(a), _toks(b)
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


def _paths(items: list[str], root: Path | None) -> set[str]:
    """File paths cited in evidence text; with a root, only paths that actually exist count."""
    out: set[str] = set()
    for it in items:
        for m in _PATH.findall(it):
            m = m.rstrip("/.,;:")
            if m and (root is None or (root / m).exists()):
                out.add(m)
    return out


STEP_MATCH = 0.2


def compare_proposals(props: list[dict], root: Path | None = None) -> dict:
    """Deterministic comparison of independent proposals.

    Steps are paired by mutual best content-word overlap (light stemming) above
    STEP_MATCH; evidence is compared by cited file paths that exist in the project.
    This measures wording overlap, not quality or correctness."""
    out: dict[str, Any] = {"proposals": props, "pairs": [],
                           "method": f"mutual-best content-word overlap ≥ {STEP_MATCH}; evidence = existing cited paths"}
    for i in range(len(props)):
        for j in range(i + 1, len(props)):
            a, b = props[i]["proposal"], props[j]["proposal"]
            sa, sb = a.get("steps", []), b.get("steps", [])
            m = [[_sim(x, y) for y in sb] for x in sa]
            agreements, used_a, used_b = [], set(), set()
            for ia in range(len(sa)):
                if not sb:
                    break
                jb = max(range(len(sb)), key=lambda k: m[ia][k])
                best_for_b = max(range(len(sa)), key=lambda k: m[k][jb])
                if best_for_b == ia and m[ia][jb] >= STEP_MATCH and jb not in used_b:
                    used_a.add(ia)
                    used_b.add(jb)
                    agreements.append({"a": sa[ia], "b": sb[jb], "similarity": round(m[ia][jb], 2)})
            ev_a, ev_b = _paths(a.get("evidence", []), root), _paths(b.get("evidence", []), root)
            ra, rb = a.get("risks", []), b.get("risks", [])
            out["pairs"].append({
                "a": props[i]["run_id"], "b": props[j]["run_id"],
                "title_similarity": round(_sim(a.get("title", ""), b.get("title", "")), 2),
                "summary_similarity": round(_sim(a.get("summary", ""), b.get("summary", "")), 2),
                "agreeing_steps": agreements,
                "only_a_steps": [x for k, x in enumerate(sa) if k not in used_a],
                "only_b_steps": [x for k, x in enumerate(sb) if k not in used_b],
                "shared_evidence": sorted(ev_a & ev_b), "only_a_evidence": sorted(ev_a - ev_b),
                "only_b_evidence": sorted(ev_b - ev_a),
                "only_a_risks": [r for r in ra if all(_sim(r, x) < STEP_MATCH for x in rb)],
                "only_b_risks": [r for r in rb if all(_sim(r, x) < STEP_MATCH for x in ra)],
            })
    return out
