"""Persistent event log.

Events are written to SQLite first (the source of truth, shared by the CLI and
the server) and then subscribers are woken. The SSE endpoint tails the table by
id, so events produced by another process (e.g. `forge run`) still stream live.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from .db import Database, dumps, loads, now


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self._waiters: set[asyncio.Event] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(
        self,
        type: str,
        message: str,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> int:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO events(ts, project_id, task_id, run_id, type, level, message, data) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (now(), project_id, task_id, run_id, type, level, message[:20000], dumps(data or {})),
            )
            eid = cur.lastrowid
        self._wake()
        return eid

    def _wake(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for w in waiters:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(w.set)
            else:
                w.set()

    async def wait(self, timeout: float) -> None:
        ev = asyncio.Event()
        with self._lock:
            self._waiters.add(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                self._waiters.discard(ev)

    def since(
        self,
        after_id: int = 0,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        for col, val in (("project_id", project_id), ("task_id", task_id), ("run_id", run_id)):
            if val:
                sql += f" AND {col} = ?"
                params.append(val)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, params)
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    def recent(self, *, project_id: str | None = None, task_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, params)
        for r in rows:
            r["data"] = loads(r["data"], {})
        return list(reversed(rows))

    def last_id(self) -> int:
        r = self.db.one("SELECT MAX(id) AS m FROM events")
        return int(r["m"] or 0) if r else 0
