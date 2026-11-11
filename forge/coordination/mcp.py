"""Minimal MCP (Model Context Protocol) server over stdio.

For local MCP clients that launch it as a subprocess (e.g. Claude Desktop,
Claude Code, Codex): `forge mcp` with FORGE_COORDINATION_TOKEN set. Every tool
call goes through CoordinationService with the token's principal, so scopes and
project limits are identical to the HTTP coordination API. This is NOT a remote
server: ChatGPT connectors require a remote HTTPS MCP endpoint, which is not
provided (see docs/REMOTE_ACCESS.md).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from .. import __version__
from .service import CoordinationError, CoordinationService, ExternalTaskRequest, Feedback
from .tokens import ScopeError, TokenService

if TYPE_CHECKING:
    from ..app import Forge

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {"name": "forge_list_projects", "description": "List Forge projects this token may access.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "forge_list_tasks", "description": "List tasks (newest first) with state, phase and assigned model.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "state": {"type": "string"},
                                                      "limit": {"type": "integer"}, "offset": {"type": "integer"}}}},
    {"name": "forge_get_task", "description": "Task detail: state, phase, runs, tests, review, approvals, commits.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "forge_get_task_events", "description": "Sanitized task event log, paginated with `after`.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "after": {"type": "integer"},
                                                      "limit": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "forge_get_task_artifacts", "description": "Structured artifacts: plans, reports, reviews, test reports.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "kind": {"type": "string"}},
                     "required": ["task_id"]}},
    {"name": "forge_list_agents", "description": "Providers, verified models and currently running agents.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "forge_submit_task", "description": "Submit a task (needs tasks:write). It may wait for the owner's "
                                                 "authorization in Forge before running.",
     "inputSchema": ExternalTaskRequest.model_json_schema()},
    {"name": "forge_submit_feedback", "description": "Comment on a task or request a revision (needs feedback:write).",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "kind": {"type": "string", "enum": ["comment", "revision"]},
         "message": {"type": "string"}}, "required": ["task_id", "kind", "message"]}},
]


class MCPServer:
    def __init__(self, forge: "Forge", token: str):
        self.svc = CoordinationService(forge)
        self.principal = TokenService(forge.db).authenticate(f"Bearer {token}")
        self.principal.source = "mcp"

    def call(self, name: str, a: dict[str, Any]) -> Any:
        p, s = self.principal, self.svc
        if name == "forge_list_projects":
            return s.projects(p)
        if name == "forge_list_tasks":
            return s.tasks(p, project=a.get("project"), state=a.get("state"), limit=int(a.get("limit", 20)),
                           offset=int(a.get("offset", 0)))
        if name == "forge_get_task":
            return s.task(p, a["task_id"])
        if name == "forge_get_task_events":
            return s.events(p, a["task_id"], after=int(a.get("after", 0)), limit=int(a.get("limit", 100)))
        if name == "forge_get_task_artifacts":
            return s.artifacts(p, a["task_id"], kind=a.get("kind"))
        if name == "forge_list_agents":
            return s.agents(p)
        if name == "forge_submit_task":
            return s.submit(p, ExternalTaskRequest.model_validate(a), source="mcp")
        if name == "forge_submit_feedback":
            return s.feedback(p, a["task_id"], Feedback(kind=a["kind"], message=a["message"]), source="mcp")
        raise KeyError(f"unknown tool {name}")

    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": "forge-coordination", "version": __version__}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params") or {}
            try:
                out = self.call(params.get("name", ""), params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}], "isError": False}}
            except (CoordinationError, ScopeError, KeyError, ValueError) as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}

    def serve_stdio(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                resp = self.handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
