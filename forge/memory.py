"""Persistent, inspectable project memory.

Retrieval combines FTS5 relevance, recency decay, importance and task affinity,
then trims to a character budget. Only the current project's entries are read,
plus entries other projects marked `shared` when the project opted in.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .db import Database, new_id, now
from .events import EventBus

KINDS = (
    "requirement", "decision", "hypothesis", "milestone", "failure", "test_result",
    "attempt", "problem", "user_decision", "note", "research",
)
KIND_WEIGHT = {
    "user_decision": 1.0, "requirement": 0.9, "decision": 0.9, "problem": 0.8, "failure": 0.75,
    "hypothesis": 0.7, "milestone": 0.6, "test_result": 0.5, "attempt": 0.5, "research": 0.6, "note": 0.4,
}
HALF_LIFE_DAYS = 14.0
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "should", "must", "will", "are", "was",
         "have", "has", "not", "you", "your", "can", "all", "any", "add", "use", "using", "make"}


class MemoryService:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus

    def add(self, project_id: str, kind: str, title: str, content: str, *, source: str = "",
            source_task_id: str | None = None, source_run_id: str | None = None,
            importance: float = 0.5, shared: bool = False, pinned: bool = False) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown memory kind {kind!r}; expected one of {KINDS}")
        if not (source or source_task_id):
            raise ValueError("every memory entry needs a source or a source task")
        mid = new_id("mem")
        self.db.execute(
            "INSERT INTO memory(id,project_id,kind,title,content,source_task_id,source_run_id,source,importance,"
            "shared,pinned,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, project_id, kind, title[:300], content[:8000], source_task_id, source_run_id, source,
             max(0.0, min(1.0, importance)), 1 if shared else 0, 1 if pinned else 0, now()),
        )
        self.bus.emit("memory.added", f"Memory [{kind}]: {title[:120]}", project_id=project_id,
                      task_id=source_task_id, data={"memory_id": mid})
        return self.db.one("SELECT * FROM memory WHERE id=?", (mid,))

    def list(self, project_id: str, kind: str | None = None, limit: int = 300) -> list[dict]:
        sql, params = "SELECT * FROM memory WHERE project_id=?", [project_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self.db.query(sql, params)

    def update(self, mid: str, **fields: Any) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("title", "content", "importance", "shared", "pinned", "kind")}
        if allowed:
            sets = ", ".join(f"{k}=?" for k in allowed)
            self.db.execute(f"UPDATE memory SET {sets} WHERE id=?", (*allowed.values(), mid))
        return self.db.one("SELECT * FROM memory WHERE id=?", (mid,))

    def delete(self, mid: str) -> None:
        self.db.execute("DELETE FROM memory WHERE id=?", (mid,))

    @staticmethod
    def _fts_query(text: str) -> str:
        words = []
        for w in _WORD.findall(text.lower()):
            if w not in _STOP and w not in words:
                words.append(w)
        return " OR ".join(f'"{w}"' for w in words[:24])

    def retrieve(self, project_id: str, query: str, *, task_id: str | None = None, k: int = 8,
                 char_budget: int = 6000, include_shared: bool = False) -> list[dict]:
        projects = [project_id]
        shared_clause = ""
        if include_shared:
            shared_clause = " OR (m.shared=1 AND m.project_id != ?)"
        q = self._fts_query(query)
        candidates: dict[str, dict] = {}
        if q:
            sql = ("SELECT m.*, bm25(memory_fts) AS rank FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
                   f"WHERE memory_fts MATCH ? AND (m.project_id = ?{shared_clause}) ORDER BY rank LIMIT 60")
            params: list[Any] = [q, *projects] + ([project_id] if include_shared else [])
            for r in self.db.query(sql, params):
                candidates[r["id"]] = r
        # Always consider pinned items, same-task items, and the most recent few.
        extra = self.db.query(
            "SELECT m.*, 0.0 AS rank FROM memory m WHERE m.project_id=? AND (m.pinned=1 OR m.source_task_id IS ?) "
            "UNION SELECT m.*, 0.0 AS rank FROM (SELECT * FROM memory WHERE project_id=? ORDER BY created_at DESC LIMIT 10) m",
            (project_id, task_id, project_id),
        )
        for r in extra:
            candidates.setdefault(r["id"], r)
        t = now()
        scored = []
        for r in candidates.values():
            relevance = 1.0 / (1.0 + math.exp(r["rank"])) if r["rank"] else 0.0  # bm25 is negative-better
            age_days = (t - r["created_at"]) / 86400
            recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
            score = (2.0 * relevance + 0.6 * recency + 0.8 * r["importance"] + 0.4 * KIND_WEIGHT.get(r["kind"], 0.4)
                     + (0.8 if task_id and r["source_task_id"] == task_id else 0) + (1.0 if r["pinned"] else 0))
            r = dict(r)
            r["score"] = round(score, 3)
            r["foreign"] = r["project_id"] != project_id
            scored.append(r)
        scored.sort(key=lambda r: r["score"], reverse=True)
        out, used = [], 0
        for r in scored:
            size = len(r["title"]) + len(r["content"])
            if len(out) >= k or used + size > char_budget:
                continue
            out.append(r)
            used += size
        return out

    @staticmethod
    def render(entries: list[dict]) -> str:
        if not entries:
            return ""
        lines = ["## Relevant project memory (retrieved; may be incomplete)"]
        for e in entries:
            src = e["source_task_id"] or e["source"]
            tag = " [shared from another project]" if e.get("foreign") else ""
            lines.append(f"- [{e['kind']}] {e['title']}{tag} (source: {src})\n  {e['content'][:1200]}")
        return "\n".join(lines)
