"""Project, task, artifact and approval services.

These are synchronous and process-agnostic: the API, CLI and engine all call
them against the same SQLite database.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from . import gitops
from .db import Database, dumps, loads, new_id, now
from .domain import (
    ACTIVE,
    TERMINAL,
    InvalidTransition,
    ProjectConfig,
    TaskState,
    can_transition,
)
from .events import EventBus

INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".github/copilot-instructions.md"]


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def suggest_test_command(root: Path) -> str:
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").is_dir():
        if (root / "uv.lock").exists():
            return "uv run --frozen pytest -q"
        return "python -m pytest -q"
    if (root / "package.json").exists():
        return "npm test --silent"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    return ""


class ProjectService:
    def __init__(self, db: Database, bus: EventBus, projects_root: Path):
        self.db = db
        self.bus = bus
        self.projects_root = projects_root

    # --- reads -------------------------------------------------------------
    def _hydrate(self, r: dict) -> dict:
        r["config"] = ProjectConfig.from_raw(loads(r["config"], {})).model_dump()
        return r

    def get(self, pid: str) -> dict:
        r = self.db.one("SELECT * FROM projects WHERE id=? OR name=?", (pid, pid))
        if not r:
            raise NotFound(f"project {pid} not found")
        return self._hydrate(r)

    def list(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM projects ORDER BY updated_at DESC")
        out = []
        for r in rows:
            r = self._hydrate(r)
            counts = self.db.query(
                "SELECT state, COUNT(*) n FROM tasks WHERE project_id=? GROUP BY state", (r["id"],)
            )
            r["task_counts"] = {c["state"]: c["n"] for c in counts}
            out.append(r)
        return out

    def config(self, pid: str) -> ProjectConfig:
        return ProjectConfig.from_raw(self.get(pid)["config"])

    # --- registration --------------------------------------------------------
    def _check_unique(self, name: str, root: Path) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", name):
            raise ValueError("project name must be 1-64 chars: letters, digits, space, . _ -")
        if self.db.one("SELECT id FROM projects WHERE name=?", (name,)):
            raise Conflict(f"a project named {name!r} already exists")
        r = self.db.one("SELECT name FROM projects WHERE root_path=?", (str(root),))
        if r:
            raise Conflict(f"{root} is already registered as project {r['name']!r}")
        # Prevent nesting: one project must not contain another, or agents of one
        # could write into the other's tree.
        for other in self.db.query("SELECT name, root_path FROM projects"):
            op = Path(other["root_path"])
            if root.is_relative_to(op) or op.is_relative_to(root):
                raise Conflict(f"{root} overlaps with project {other['name']!r} at {op}")

    def _insert(self, name: str, root: Path, origin: str, description: str, cfg: ProjectConfig,
                instructions: str = "") -> dict:
        pid = new_id("p")
        t = now()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO projects(id,name,root_path,description,origin,status,instructions,config,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, name, str(root), description, origin, "active", instructions, dumps(cfg.model_dump()), t, t),
            )
            for typ, enabled in (("git", 1), ("local_files", 1), ("testing", 1 if cfg.test_command else 0)):
                c.execute(
                    "INSERT INTO project_integrations(project_id,type,enabled,config) VALUES (?,?,?,?)",
                    (pid, typ, enabled, "{}"),
                )
        self.scan_instruction_files(pid)
        self.bus.emit("project.registered", f"Registered project {name} ({origin})", project_id=pid,
                      data={"root": str(root), "origin": origin})
        return self.get(pid)

    def create(self, name: str, description: str = "", path: str | None = None,
               config: dict | None = None, instructions: str = "") -> dict:
        root = Path(path).expanduser().resolve() if path else (self.projects_root / name.replace(" ", "-")).resolve()
        self._check_unique(name, root)
        if root.exists() and any(root.iterdir()):
            raise Conflict(f"{root} exists and is not empty; use import instead")
        gitops.init_repo(root)
        (root / "README.md").write_text(f"# {name}\n\n{description}\n")
        gitops.commit_all(root, "Initial commit (created by Forge)")
        cfg = ProjectConfig.from_raw(config)
        return self._insert(name, root, "created", description, cfg, instructions)

    def import_folder(self, path: str, name: str | None = None, description: str = "",
                      config: dict | None = None, instructions: str = "") -> dict:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"{root} is not a directory")
        if gitops.is_repo(root):
            root = gitops.repo_root(root)
        name = name or root.name
        self._check_unique(name, root)
        cfg = ProjectConfig.from_raw(config)
        is_git = gitops.is_repo(root)
        if not is_git or not gitops.has_commits(root):
            # Without commits there is nothing to branch from, so agent writes cannot be
            # isolated. Fall back to read-only rather than touching the user's folder.
            cfg.permissions.mode = "read_only"
        origin = "imported_git" if is_git else "imported_folder"
        return self._insert(name, root, origin, description, cfg, instructions)

    def import_git(self, url: str, name: str | None = None, description: str = "",
                   config: dict | None = None) -> dict:
        if Path(url).expanduser().exists():
            return self.import_folder(url, name, description, config)
        if not re.match(r"^(https://|git@|ssh://)", url):
            raise ValueError("expected a local path or an https/ssh git URL")
        name = name or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
        dest = (self.projects_root / name).resolve()
        self._check_unique(name, dest)
        p = subprocess.run(["git", "clone", "--", url, str(dest)], capture_output=True, text=True, timeout=600,
                           env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
        if p.returncode != 0:
            raise ValueError(f"git clone failed: {p.stderr.strip()[:500]}")
        return self._insert(name, dest, "imported_git", description, ProjectConfig.from_raw(config))

    # --- updates -------------------------------------------------------------
    def update(self, pid: str, *, description: str | None = None, instructions: str | None = None,
               config: dict | None = None) -> dict:
        p = self.get(pid)
        cfg = p["config"]
        if config:
            merged = _deep_merge(cfg, config)
            cfg = ProjectConfig.from_raw(merged).model_dump()
        with self.db.tx() as c:
            c.execute(
                "UPDATE projects SET description=?, instructions=?, config=?, updated_at=? WHERE id=?",
                (description if description is not None else p["description"],
                 instructions if instructions is not None else p["instructions"],
                 dumps(cfg), now(), p["id"]),
            )
        self.bus.emit("project.updated", f"Updated settings for {p['name']}", project_id=p["id"])
        return self.get(p["id"])

    def set_status(self, pid: str, status: str) -> dict:
        p = self.get(pid)
        self.db.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", (status, now(), p["id"]))
        self.bus.emit(f"project.{status}", f"Project {p['name']} is now {status}", project_id=p["id"])
        return self.get(p["id"])

    def remove(self, pid: str) -> None:
        """Unregister only. Never deletes the project's files."""
        p = self.get(pid)
        active = self.db.one(
            "SELECT COUNT(*) n FROM tasks WHERE project_id=? AND state IN (%s)" % ",".join("?" * len(ACTIVE)),
            (p["id"], *[s.value for s in ACTIVE]),
        )
        if active and active["n"]:
            raise Conflict("project has active tasks; cancel them first")
        self.db.execute("DELETE FROM projects WHERE id=?", (p["id"],))

    # --- instruction files (untrusted until approved) ------------------------
    def scan_instruction_files(self, pid: str) -> list[dict]:
        p = self.get(pid)
        root = Path(p["root_path"])
        found = []
        with self.db.tx() as c:
            for rel in INSTRUCTION_FILES:
                f = root / rel
                if not f.is_file():
                    continue
                sha = _sha(f)
                prev = c.execute("SELECT sha256, trusted FROM instruction_files WHERE project_id=? AND path=?",
                                 (p["id"], rel)).fetchone()
                if prev is None:
                    c.execute("INSERT INTO instruction_files(project_id,path,sha256,trusted) VALUES (?,?,?,0)",
                              (p["id"], rel, sha))
                elif prev["sha256"] != sha:
                    # Content changed since approval -> untrusted again.
                    c.execute("UPDATE instruction_files SET sha256=?, trusted=0, approved_at=NULL "
                              "WHERE project_id=? AND path=?", (sha, p["id"], rel))
                found.append(rel)
        return self.instruction_files(pid)

    def instruction_files(self, pid: str) -> list[dict]:
        p = self.get(pid)
        rows = self.db.query("SELECT * FROM instruction_files WHERE project_id=? ORDER BY path", (p["id"],))
        root = Path(p["root_path"])
        for r in rows:
            f = root / r["path"]
            r["exists"] = f.is_file()
            r["size"] = f.stat().st_size if f.is_file() else 0
        return rows

    def trust_instruction_file(self, pid: str, rel: str, trusted: bool) -> None:
        p = self.get(pid)
        f = Path(p["root_path"]) / rel
        if not f.is_file():
            raise NotFound(rel)
        self.db.execute(
            "UPDATE instruction_files SET trusted=?, sha256=?, approved_at=? WHERE project_id=? AND path=?",
            (1 if trusted else 0, _sha(f), now() if trusted else None, p["id"], rel),
        )
        self.bus.emit("project.instructions", f"{'Trusted' if trusted else 'Untrusted'} {rel}", project_id=p["id"])

    def trusted_instructions(self, pid: str) -> str:
        """User-authored instructions + repository files the user explicitly approved (hash-pinned)."""
        p = self.get(pid)
        parts = []
        if p["instructions"].strip():
            parts.append("## Project instructions (from the Forge user)\n" + p["instructions"].strip())
        root = Path(p["root_path"])
        for r in self.db.query("SELECT * FROM instruction_files WHERE project_id=? AND trusted=1", (p["id"],)):
            f = root / r["path"]
            if f.is_file() and _sha(f) == r["sha256"]:
                parts.append(f"## {r['path']} (approved by user)\n" + f.read_text(errors="replace")[:20000])
        return "\n\n".join(parts)

    # --- integrations ---------------------------------------------------------
    def integrations(self, pid: str) -> list[dict]:
        p = self.get(pid)
        rows = self.db.query("SELECT * FROM project_integrations WHERE project_id=?", (p["id"],))
        for r in rows:
            r["config"] = loads(r["config"], {})
            r["enabled"] = bool(r["enabled"])
        return rows

    def set_integration(self, pid: str, typ: str, enabled: bool, config: dict) -> None:
        p = self.get(pid)
        self.db.execute(
            "INSERT INTO project_integrations(project_id,type,enabled,config) VALUES (?,?,?,?) "
            "ON CONFLICT(project_id,type) DO UPDATE SET enabled=excluded.enabled, config=excluded.config",
            (p["id"], typ, 1 if enabled else 0, dumps(config)),
        )
        self.bus.emit("project.integration", f"Integration {typ} {'enabled' if enabled else 'disabled'}",
                      project_id=p["id"])

    # --- milestones -----------------------------------------------------------
    def milestones(self, pid: str) -> list[dict]:
        p = self.get(pid)
        return self.db.query("SELECT * FROM milestones WHERE project_id=? ORDER BY position, created_at", (p["id"],))

    def add_milestone(self, pid: str, title: str, description: str = "", task_id: str | None = None) -> dict:
        p = self.get(pid)
        mid = new_id("m")
        pos = (self.db.one("SELECT COALESCE(MAX(position),0)+1 n FROM milestones WHERE project_id=?", (p["id"],)) or {}).get("n", 1)
        self.db.execute(
            "INSERT INTO milestones(id,project_id,title,description,status,task_id,position,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (mid, p["id"], title, description, "open", task_id, pos, now()),
        )
        return self.db.one("SELECT * FROM milestones WHERE id=?", (mid,))

    def set_milestone_status(self, mid: str, status: str) -> None:
        self.db.execute("UPDATE milestones SET status=? WHERE id=?", (status, mid))


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


TASK_JSON_FIELDS = ("acceptance_criteria", "routing_overrides", "allowed_tools", "budget", "metadata")


class TaskService:
    def __init__(self, db: Database, bus: EventBus, projects: ProjectService):
        self.db = db
        self.bus = bus
        self.projects = projects

    def _hydrate(self, r: dict) -> dict:
        for f in TASK_JSON_FIELDS:
            r[f] = loads(r[f], [] if f in ("acceptance_criteria", "allowed_tools") else {})
        r["depends_on"] = [d["depends_on"] for d in
                           self.db.query("SELECT depends_on FROM task_deps WHERE task_id=?", (r["id"],))]
        return r

    def get(self, tid: str) -> dict:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise NotFound(f"task {tid} not found")
        return self._hydrate(r)

    def list(self, project_id: str | None = None, states: list[str] | None = None, limit: int = 200,
             parent_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(self.projects.get(project_id)["id"])
        if states:
            sql += " AND state IN (%s)" % ",".join("?" * len(states))
            params.extend(states)
        if parent_id:
            sql += " AND parent_id=?"
            params.append(parent_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._hydrate(r) for r in self.db.query(sql, params)]

    def create(self, project_id: str, *, title: str, objective: str, workflow: str,
               acceptance_criteria: list[str] | None = None, category: str = "feature",
               depends_on: list[str] | None = None, parent_id: str | None = None,
               complexity: str = "medium", criticality: str = "normal", routing_mode: str | None = None,
               routing_overrides: dict | None = None, review_depth: str = "standard",
               approval_policy: str = "risky", execution_mode: str | None = None,
               allowed_tools: list[str] | None = None, budget: dict | None = None,
               metadata: dict | None = None, start: bool = True) -> dict:
        p = self.projects.get(project_id)
        cfg = ProjectConfig.from_raw(p["config"])
        mode = execution_mode or ("read_only" if cfg.permissions.mode == "read_only" else "worktree")
        if cfg.permissions.mode == "read_only" and mode != "read_only":
            raise PermissionError("project is read-only; tasks cannot use worktree_write execution")
        for d in depends_on or []:
            dep = self.get(d)
            if dep["project_id"] != p["id"]:
                raise ValueError("dependencies must belong to the same project")
        tid = new_id("t")
        t = now()
        lim = cfg.limits.model_dump()
        lim.update(budget or {})
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO tasks(id,project_id,parent_id,title,objective,acceptance_criteria,category,workflow,
                state,complexity,criticality,routing_mode,routing_overrides,review_depth,approval_policy,
                execution_mode,allowed_tools,budget,metadata,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, p["id"], parent_id, title, objective, dumps(acceptance_criteria or []), category, workflow,
                 TaskState.CREATED.value, complexity, criticality, routing_mode or cfg.default_routing_mode,
                 dumps(routing_overrides or {}), review_depth, approval_policy, mode, dumps(allowed_tools or []),
                 dumps(lim), dumps(metadata or {}), t, t),
            )
            for d in depends_on or []:
                c.execute("INSERT INTO task_deps(task_id, depends_on) VALUES (?,?)", (tid, d))
        self.bus.emit("task.created", f"Task created: {title}", project_id=p["id"], task_id=tid,
                      data={"workflow": workflow})
        if start:
            self.transition(tid, TaskState.READY, "queued")
        return self.get(tid)

    def transition(self, tid: str, to: TaskState | str, reason: str = "", **fields: Any) -> dict:
        to = TaskState(to)
        with self.db.tx() as c:
            r = c.execute("SELECT state, project_id FROM tasks WHERE id=?", (tid,)).fetchone()
            if not r:
                raise NotFound(tid)
            frm = TaskState(r["state"])
            if frm != to and not can_transition(frm, to):
                raise InvalidTransition(f"{frm} -> {to} not allowed")
            sets = ["state=?", "updated_at=?"]
            vals: list[Any] = [to.value, now()]
            if to in TERMINAL:
                sets.append("finished_at=?")
                vals.append(now())
            if to in ACTIVE:
                sets.append("started_at=COALESCE(started_at, ?)")
                vals.append(now())
            for k, v in fields.items():
                sets.append(f"{k}=?")
                vals.append(dumps(v) if isinstance(v, (dict, list)) else v)
            vals.append(tid)
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        if frm != to:
            self.bus.emit("task.state", f"{frm.value} → {to.value}" + (f": {reason}" if reason else ""),
                          project_id=r["project_id"], task_id=tid,
                          data={"from": frm.value, "to": to.value, "reason": reason})
        return self.get(tid)

    def update_fields(self, tid: str, **fields: Any) -> None:
        if not fields:
            return
        sets = [f"{k}=?" for k in fields] + ["updated_at=?"]
        vals = [dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()] + [now(), tid]
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)

    def deps_satisfied(self, tid: str) -> tuple[bool, list[str]]:
        rows = self.db.query(
            "SELECT t.id, t.state FROM task_deps d JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=?", (tid,))
        unmet = [r["id"] for r in rows if r["state"] != TaskState.COMPLETED.value]
        failed = [r["id"] for r in rows if r["state"] in (TaskState.FAILED.value, TaskState.CANCELLED.value)]
        return (not unmet, failed)

    # --- user controls (honoured by the engine, safe from any process) --------
    def request_pause(self, tid: str) -> dict:
        t = self.get(tid)
        st = TaskState(t["state"])
        if st in TERMINAL or st == TaskState.PAUSED:
            return t
        if st in ACTIVE:
            self.update_fields(tid, pause_requested=1)
            self.bus.emit("task.pause_requested", "Pause requested; will pause after the current step is interrupted",
                          project_id=t["project_id"], task_id=tid)
            return self.get(tid)
        return self.transition(tid, TaskState.PAUSED, "paused by user", resume_state=st.value, pause_requested=0)

    def resume(self, tid: str) -> dict:
        t = self.get(tid)
        if t["state"] != TaskState.PAUSED.value:
            self.update_fields(tid, pause_requested=0)
            return self.get(tid)
        back = t["resume_state"] or TaskState.READY.value
        if back in {s.value for s in ACTIVE}:
            back = TaskState.READY.value
        return self.transition(tid, back, "resumed by user", pause_requested=0, resume_state=None)

    def request_cancel(self, tid: str) -> dict:
        t = self.get(tid)
        st = TaskState(t["state"])
        if st in TERMINAL:
            return t
        if st in ACTIVE:
            self.update_fields(tid, cancel_requested=1)
            self.bus.emit("task.cancel_requested", "Cancellation requested", project_id=t["project_id"], task_id=tid)
            return self.get(tid)
        return self.transition(tid, TaskState.CANCELLED, "cancelled by user", disposition="cancelled by user")

    def retry(self, tid: str) -> dict:
        t = self.get(tid)
        if t["state"] != TaskState.FAILED.value:
            raise InvalidTransition("only FAILED tasks can be retried")
        return self.transition(tid, TaskState.READY, "retry requested by user", error=None, attempts=0)


class ArtifactService:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus

    def add(self, project_id: str, task_id: str | None, kind: str, title: str, content: str | dict | list,
            run_id: str | None = None) -> dict:
        ctype = "application/json" if isinstance(content, (dict, list)) else "text/markdown"
        if kind == "diff":
            ctype = "text/x-diff"
        body = dumps(content) if isinstance(content, (dict, list)) else content
        aid = new_id("a")
        self.db.execute(
            "INSERT INTO artifacts(id,project_id,task_id,run_id,kind,title,content,content_type,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, project_id, task_id, run_id, kind, title, body, ctype, now()),
        )
        self.bus.emit("artifact.created", f"Artifact: {title}", project_id=project_id, task_id=task_id, run_id=run_id,
                      data={"artifact_id": aid, "kind": kind})
        return self.get(aid)

    def get(self, aid: str) -> dict:
        r = self.db.one("SELECT * FROM artifacts WHERE id=?", (aid,))
        if not r:
            raise NotFound(aid)
        if r["content_type"] == "application/json":
            r["data"] = loads(r["content"])
        return r

    def list(self, *, project_id: str | None = None, task_id: str | None = None, kind: str | None = None,
             limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM artifacts WHERE 1=1", []
        for col, v in (("project_id", project_id), ("task_id", task_id), ("kind", kind)):
            if v:
                sql += f" AND {col}=?"
                params.append(v)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, params)
        for r in rows:
            if r["content_type"] == "application/json":
                r["data"] = loads(r["content"])
        return rows

    def latest(self, task_id: str, kind: str) -> dict | None:
        r = self.db.one("SELECT * FROM artifacts WHERE task_id=? AND kind=? ORDER BY created_at DESC LIMIT 1",
                        (task_id, kind))
        if r and r["content_type"] == "application/json":
            r["data"] = loads(r["content"])
        return r


class ApprovalService:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus

    def request(self, project_id: str, task_id: str | None, kind: str, title: str, detail: str = "",
                payload: dict | None = None) -> dict:
        existing = self.db.one(
            "SELECT * FROM approvals WHERE task_id IS ? AND kind=? AND status='pending'", (task_id, kind))
        if existing:
            return existing
        aid = new_id("ap")
        self.db.execute(
            "INSERT INTO approvals(id,project_id,task_id,kind,title,detail,payload,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, project_id, task_id, kind, title, detail, dumps(payload or {}), "pending", now()),
        )
        self.bus.emit("approval.requested", f"Approval needed: {title}", project_id=project_id, task_id=task_id,
                      level="warn", data={"approval_id": aid, "kind": kind})
        return self.get(aid)

    def get(self, aid: str) -> dict:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (aid,))
        if not r:
            raise NotFound(aid)
        r["payload"] = loads(r["payload"], {})
        return r

    def list(self, *, project_id: str | None = None, status: str | None = None, task_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM approvals WHERE 1=1", []
        for col, v in (("project_id", project_id), ("status", status), ("task_id", task_id)):
            if v:
                sql += f" AND {col}=?"
                params.append(v)
        sql += " ORDER BY created_at DESC"
        rows = self.db.query(sql, params)
        for r in rows:
            r["payload"] = loads(r["payload"], {})
        return rows

    def decide(self, aid: str, approve: bool, note: str = "") -> dict:
        a = self.get(aid)
        if a["status"] != "pending":
            raise Conflict(f"approval already {a['status']}")
        status = "approved" if approve else "rejected"
        self.db.execute("UPDATE approvals SET status=?, decided_at=?, decision_note=? WHERE id=?",
                        (status, now(), note, aid))
        self.bus.emit("approval.decided", f"{a['title']}: {status}", project_id=a["project_id"], task_id=a["task_id"],
                      data={"approval_id": aid, "status": status, "kind": a["kind"]})
        return self.get(aid)
