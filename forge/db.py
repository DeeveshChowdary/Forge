"""SQLite persistence.

A single database file holds every project's state. Rows are always scoped by
project_id; services never query across projects unless the user explicitly
enabled shared knowledge. WAL mode lets the CLI and the server share the file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,                -- created | imported_folder | imported_git
    status TEXT NOT NULL DEFAULT 'active', -- active | paused | archived
    instructions TEXT NOT NULL DEFAULT '',  -- user-authored, trusted
    config TEXT NOT NULL DEFAULT '{}',    -- permissions, limits, test command, routing
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_integrations (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, type)
);

CREATE TABLE IF NOT EXISTS instruction_files (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    trusted INTEGER NOT NULL DEFAULT 0,  -- repository instructions are untrusted until approved
    approved_at REAL,
    PRIMARY KEY (project_id, path)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES tasks(id),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT 'feature',
    workflow TEXT NOT NULL,
    state TEXT NOT NULL,
    phase TEXT,                     -- current workflow phase key
    phase_index INTEGER NOT NULL DEFAULT 0,
    complexity TEXT NOT NULL DEFAULT 'medium',
    criticality TEXT NOT NULL DEFAULT 'normal',
    routing_mode TEXT NOT NULL DEFAULT 'balanced',
    routing_overrides TEXT NOT NULL DEFAULT '{}',  -- {phase: {provider, model, effort}}
    review_depth TEXT NOT NULL DEFAULT 'standard',  -- none | standard | independent
    approval_policy TEXT NOT NULL DEFAULT 'risky',   -- risky | every_phase | never_merge
    execution_mode TEXT NOT NULL DEFAULT 'worktree', -- worktree | read_only
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    budget TEXT NOT NULL DEFAULT '{}',
    assigned_provider TEXT,
    assigned_model TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    review_cycle INTEGER NOT NULL DEFAULT 0,
    worktree_path TEXT,
    branch TEXT,
    base_commit TEXT,
    disposition TEXT,
    error TEXT,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    resume_state TEXT,              -- state to return to after PAUSED
    lease_owner TEXT,
    lease_expires REAL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, state);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    role TEXT NOT NULL,             -- planner | implementer | tester | reviewer | proposer ...
    kind TEXT NOT NULL DEFAULT 'agent', -- agent | test | system
    provider TEXT,
    model TEXT,
    resolved_model TEXT,            -- exact model id reported by the provider, if any
    effort TEXT,
    state TEXT NOT NULL,            -- running | succeeded | failed | cancelled | interrupted
    attempt INTEGER NOT NULL DEFAULT 1,
    review_cycle INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    prompt TEXT,
    output_text TEXT,
    structured_output TEXT,
    usage TEXT NOT NULL DEFAULT '{}',
    exit_code INTEGER,
    error TEXT,
    log_path TEXT,
    pid INTEGER,
    engine_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT,
    kind TEXT NOT NULL,   -- plan | handoff | diff | test_report | review | proposal | log | note
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/markdown',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project_id TEXT,
    task_id TEXT,
    run_id TEXT,
    type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);

CREATE TABLE IF NOT EXISTS routing_decisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT,
    phase TEXT NOT NULL,
    mode TEXT NOT NULL,
    policy TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    effort TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    candidates TEXT NOT NULL DEFAULT '[]',
    outcome TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,     -- merge | paid_api | destructive | publish | review_unresolved | phase | instructions
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    decided_at REAL,
    decision_note TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT NOT NULL,         -- discovered | configured
    status TEXT NOT NULL DEFAULT 'unverified', -- unverified | verified | failed
    resolved_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '{}',
    profile TEXT NOT NULL DEFAULT '{}',   -- tier, cost_index, strengths (user editable)
    enabled INTEGER NOT NULL DEFAULT 1,
    preference REAL NOT NULL DEFAULT 0,
    verified_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (provider, model_id)
);

CREATE TABLE IF NOT EXISTS provider_status (
    provider TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    checked_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_task_id TEXT,
    source_run_id TEXT,
    source TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    shared INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title, content, content='memory', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE TABLE IF NOT EXISTS milestones (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',   -- open | done
    task_id TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- Coordination layer (M9). Additive only: older databases gain these tables on open.
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,    -- sha256 of the token; the token itself is never stored
    prefix TEXT NOT NULL,               -- first characters, for identification in the UI
    scopes TEXT NOT NULL DEFAULT '["read"]',
    projects TEXT NOT NULL DEFAULT '[]', -- project ids, or ["*"]
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS gh_links (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_url TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    task_id TEXT,
    status TEXT NOT NULL,               -- discovered | validated | awaiting_authorization | queued | running | completed | rejected | failed
    reason TEXT NOT NULL DEFAULT '',
    request_hash TEXT NOT NULL DEFAULT '',
    status_comment_id INTEGER,
    last_published TEXT NOT NULL DEFAULT '',
    last_published_at REAL,
    comments_since TEXT,
    error TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (repo, issue_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gh_links_task ON gh_links(task_id) WHERE task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS gh_comments (
    repo TEXT NOT NULL,
    comment_id INTEGER NOT NULL,
    issue_number INTEGER NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    processed_at REAL NOT NULL,
    PRIMARY KEY (repo, comment_id)
);

CREATE TABLE IF NOT EXISTS gh_outbox (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,    -- one message per (link, event) even across restarts
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    comment_id INTEGER,
    created_at REAL NOT NULL,
    sent_at REAL
);

CREATE TABLE IF NOT EXISTS worker_locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    pid INTEGER NOT NULL,
    heartbeat REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    pid INTEGER NOT NULL,
    heartbeat REAL NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def dumps(v: Any) -> str:
    return json.dumps(v, default=str)


def loads(v: str | None, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class Database:
    """Thread-safe wrapper: one connection per thread, explicit transactions."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._conn().executescript(SCHEMA)  # executescript manages its own transaction
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE transaction: serialises writers across processes."""
        c = self._conn()
        if c.in_transaction:  # nested use joins the outer transaction
            yield c
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        r = self._conn().execute(sql, params).fetchone()
        return dict(r) if r else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.tx() as c:
            return c.execute(sql, params).rowcount

    # --- settings -------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return loads(r["value"], default) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
