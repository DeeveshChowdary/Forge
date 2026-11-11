"""Scoped API tokens for external assistants.

Tokens are random, shown once at creation, and stored only as SHA-256 hashes.
Scopes: `read` (default), `tasks:write` (submit tasks), `feedback:write`
(comments and revision requests). Every token is limited to listed projects
(or `*`). There is no scope that approves merges, spending or other sensitive
actions: those stay with the human in the Forge UI/CLI.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field

from ..db import Database, dumps, loads, new_id, now

SCOPES = ("read", "tasks:write", "feedback:write")


class AuthError(Exception):
    status = 401


class ScopeError(Exception):
    status = 403


@dataclass
class Principal:
    token_id: str
    name: str
    scopes: set[str]
    projects: list[str]                 # ["*"] = all
    source: str = "api"                 # api | github | mcp
    extra: dict = field(default_factory=dict)

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ScopeError(f"token '{self.name}' lacks scope '{scope}'")

    def can_access(self, project_id: str) -> bool:
        return "*" in self.projects or project_id in self.projects

    def require_project(self, project_id: str) -> None:
        if not self.can_access(project_id):
            raise ScopeError(f"token '{self.name}' is not authorized for project {project_id}")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenService:
    def __init__(self, db: Database):
        self.db = db

    def create(self, name: str, scopes: list[str] | None = None, projects: list[str] | None = None) -> dict:
        scopes = scopes or ["read"]
        bad = [s for s in scopes if s not in SCOPES]
        if bad:
            raise ValueError(f"unknown scope(s) {bad}; valid: {SCOPES}")
        if "read" not in scopes:
            scopes = ["read", *scopes]
        if not projects:
            raise ValueError("a token must be limited to at least one project (or '*')")
        token = "fgt_" + secrets.token_urlsafe(32)
        tid = new_id("tok")
        self.db.execute(
            "INSERT INTO api_tokens(id,name,token_hash,prefix,scopes,projects,created_at) VALUES (?,?,?,?,?,?,?)",
            (tid, name[:80], _hash(token), token[:10], dumps(sorted(set(scopes))), dumps(projects), now()))
        return {**self.get(tid), "token": token}  # the only time the plaintext token is returned

    def get(self, tid: str) -> dict:
        r = self.db.one("SELECT id,name,prefix,scopes,projects,created_at,last_used_at,revoked_at FROM api_tokens "
                        "WHERE id=?", (tid,))
        if not r:
            raise KeyError(tid)
        r["scopes"], r["projects"] = loads(r["scopes"], []), loads(r["projects"], [])
        return r

    def list(self) -> list[dict]:
        return [self.get(r["id"]) for r in self.db.query("SELECT id FROM api_tokens ORDER BY created_at DESC")]

    def revoke(self, tid: str) -> None:
        self.db.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now(), tid))

    def authenticate(self, header: str | None) -> Principal:
        if not header or not header.lower().startswith("bearer "):
            raise AuthError("missing bearer token")
        token = header.split(None, 1)[1].strip()
        r = self.db.one("SELECT * FROM api_tokens WHERE token_hash=?", (_hash(token),))
        if not r or r["revoked_at"]:
            raise AuthError("invalid or revoked token")
        self.db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now(), r["id"]))
        return Principal(r["id"], r["name"], set(loads(r["scopes"], [])), loads(r["projects"], []))


class RateLimiter:
    """Per-key token bucket (in-process)."""

    def __init__(self, per_minute: int = 120):
        self.rate = per_minute / 60.0
        self.capacity = float(per_minute)
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, t))
            tokens = min(self.capacity, tokens + (t - last) * self.rate)
            if tokens < 1:
                self._state[key] = (tokens, t)
                return False
            self._state[key] = (tokens - 1, t)
            return True
