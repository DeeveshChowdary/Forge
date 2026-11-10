"""Generic, per-project integration system.

Each integration declares a config schema, a status check and optional
read-only actions. Integrations are enabled per project and never become hard
Forge dependencies. Nothing here writes to external systems.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import httpx

from . import gitops

if TYPE_CHECKING:
    from .app import Forge

MAX_FILE_BYTES = 512 * 1024
HIDDEN = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".DS_Store"}


def safe_path(root: Path, rel: str) -> Path:
    """Resolve rel under root; refuse traversal and symlink escapes."""
    root = root.resolve()
    p = (root / (rel or ".")).resolve()
    if not p.is_relative_to(root):
        raise PermissionError("path escapes the project root")
    if any(part == ".git" for part in p.relative_to(root).parts):
        raise PermissionError(".git internals are not exposed")
    return p


def list_dir(root: Path, rel: str = "") -> list[dict]:
    d = safe_path(root, rel)
    if not d.is_dir():
        raise NotADirectoryError(rel)
    out = []
    for c in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if c.name in HIDDEN:
            continue
        try:
            st = c.stat()
        except OSError:
            continue
        out.append({"name": c.name, "path": str(c.relative_to(root.resolve())), "dir": c.is_dir(),
                    "size": st.st_size if c.is_file() else None, "mtime": st.st_mtime})
    return out


def read_file(root: Path, rel: str) -> dict:
    f = safe_path(root, rel)
    if not f.is_file():
        raise FileNotFoundError(rel)
    size = f.stat().st_size
    data = f.read_bytes()[:MAX_FILE_BYTES]
    binary = b"\x00" in data[:4096]
    return {"path": rel, "size": size, "truncated": size > MAX_FILE_BYTES, "binary": binary,
            "content": "" if binary else data.decode(errors="replace")}


class Integration:
    type: str = ""
    name: str = ""
    description: str = ""
    config_fields: list[dict] = []
    actions: dict[str, str] = {}

    def status(self, forge: "Forge", project: dict, config: dict) -> dict[str, Any]:
        return {"ok": True}

    def run_action(self, forge: "Forge", project: dict, config: dict, action: str, args: dict) -> Any:
        raise ValueError(f"unknown action {action}")


class GitIntegration(Integration):
    type, name = "git", "Git"
    description = "Branches, worktrees and history of the project repository (read-only)."

    def status(self, forge, project, config):
        root = Path(project["root_path"])
        if not gitops.is_repo(root):
            return {"ok": False, "detail": "not a git repository"}
        has = gitops.has_commits(root)
        wts = gitops.git(root, "worktree", "list", "--porcelain", check=False).count("worktree ")
        return {"ok": True, "branch": gitops.current_branch(root), "head": gitops.head_commit(root)[:10] if has else None,
                "uncommitted": len(gitops.dirty_files(root)), "worktrees": wts, "has_commits": has}


class LocalFilesIntegration(Integration):
    type, name = "local_files", "Local files"
    description = "Browse and read files inside the project root (path-traversal protected)."


class TestingIntegration(Integration):
    type, name = "testing", "Testing"
    description = "Runs the project's test command in task worktrees after implementation."

    def status(self, forge, project, config):
        cmd = project["config"].get("test_command", "")
        return {"ok": bool(cmd), "command": cmd or None,
                "detail": "configured" if cmd else "no test command; tasks will record 'no test evidence'"}


class GitHubIntegration(Integration):
    type, name = "github", "GitHub"
    description = "Detects the GitHub remote and gh CLI. Forge never pushes or publishes without approval."

    def status(self, forge, project, config):
        root = Path(project["root_path"])
        remote = gitops.git(root, "remote", "get-url", "origin", check=False).strip() if gitops.is_repo(root) else ""
        gh = shutil.which("gh")
        return {"ok": bool(remote), "remote": remote or None, "gh_cli": gh,
                "detail": ("gh CLI not installed; GitHub API features unavailable" if not gh else "gh CLI found")
                + "; push/PR creation requires explicit approval and is not automated in this version"}


class BrowserIntegration(Integration):
    type, name = "browser", "Browser / web research"
    description = "Allows agents to use web search/fetch tools. Off by default; enabling is a permission change."

    def status(self, forge, project, config):
        allowed = project["config"]["permissions"].get("allow_network_tools", False)
        return {"ok": True, "agents_may_use_network": allowed,
                "detail": "enable via project permissions → allow_network_tools"}


class MCPIntegration(Integration):
    type, name = "mcp", "MCP servers"
    description = "Pass an MCP config file (JSON) to Claude Code runs for this project."
    config_fields = [{"key": "config_path", "label": "MCP config JSON path", "type": "path"}]

    def status(self, forge, project, config):
        p = config.get("config_path")
        if not p:
            return {"ok": False, "detail": "no MCP config path set"}
        f = Path(p).expanduser()
        if not f.is_file():
            return {"ok": False, "detail": f"{f} not found"}
        try:
            servers = list((json.loads(f.read_text()).get("mcpServers") or {}).keys())
        except ValueError as e:
            return {"ok": False, "detail": f"invalid JSON: {e}"}
        return {"ok": True, "servers": servers,
                "detail": "passed to Claude Code via --mcp-config (Codex runs do not receive it in this version)"}


class ZoteroIntegration(Integration):
    """Read-only Zotero access.

    backend=local_api: Forge's own read-only client for Zotero's official local
      API (http://127.0.0.1:23119/api/, reads need no key). Never writes.
    backend=r4_cli: delegates to an existing project's `r4` CLI (e.g. the R4
      research project) for its read commands only (status/search/get), reusing
      that project's integration without touching its credentials or database.
    """

    type, name = "zotero", "Zotero (read-only)"
    description = "Search the local Zotero library. Optional; never writes, never handles credentials."
    config_fields = [
        {"key": "backend", "label": "Backend", "type": "select", "options": ["local_api", "r4_cli"]},
        {"key": "base_url", "label": "Local API base", "type": "text", "default": "http://127.0.0.1:23119/api/"},
        {"key": "r4_project_path", "label": "Project with r4 CLI", "type": "path"},
    ]
    actions = {"search": "Search references (read-only)"}
    R4_READ_COMMANDS = {"status", "search", "get"}

    def _base(self, config):
        base = config.get("base_url") or "http://127.0.0.1:23119/api/"
        if not base.startswith(("http://127.0.0.1", "http://localhost")):
            raise PermissionError("Zotero integration only talks to the local API on this machine")
        return base.rstrip("/") + "/"

    def _r4(self, config, *args: str) -> dict:
        root = Path(config.get("r4_project_path", "")).expanduser()
        if args[0] not in self.R4_READ_COMMANDS:
            raise PermissionError(f"r4 command {args[0]!r} is not a read-only command")
        if not (root / "pyproject.toml").is_file() or not shutil.which("uv"):
            raise FileNotFoundError("r4 project or uv not found")
        p = subprocess.run([shutil.which("uv"), "run", "--frozen", "r4", *args], cwd=root, capture_output=True,
                           text=True, timeout=60)
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"error": (p.stderr or p.stdout)[-500:]}

    def status(self, forge, project, config):
        if config.get("backend") == "r4_cli":
            try:
                r = self._r4(config, "status")
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "backend": "r4_cli", "detail": str(e)}
            return {"ok": "error" not in r, "backend": "r4_cli", "detail": r.get("error") or "r4 status ok",
                    "collections": [c.get("name") for c in r.get("collections", [])][:20]}
        try:
            base = self._base(config)
            r = httpx.get(base, timeout=2.0)
            return {"ok": r.status_code == 200, "backend": "local_api", "api_version": r.headers.get("Zotero-API-Version"),
                    "detail": "local API reachable (read-only)" if r.status_code == 200 else f"HTTP {r.status_code}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "local_api", "detail": f"Zotero local API not reachable ({type(e).__name__}); "
                                                                  "start Zotero to enable"}

    def run_action(self, forge, project, config, action, args):
        if action != "search":
            raise ValueError("only 'search' is supported (read-only)")
        q = str(args.get("q", ""))[:200]
        if config.get("backend") == "r4_cli":
            return self._r4(config, "search", q)
        base = self._base(config)
        h = httpx.get(base, timeout=3.0)
        sid = h.headers.get("Zotero-Server-ID")
        r = httpx.get(base + "users/0/items/top", params={"q": q, "limit": 15, "format": "json"},
                      headers={"Zotero-Server-ID": sid} if sid else {}, timeout=10.0)
        r.raise_for_status()
        return [{"key": i.get("key"), "title": i.get("data", {}).get("title"),
                 "date": i.get("data", {}).get("date"), "type": i.get("data", {}).get("itemType")} for i in r.json()]


REGISTRY: dict[str, Integration] = {i.type: i for i in [
    GitIntegration(), LocalFilesIntegration(), TestingIntegration(), GitHubIntegration(), BrowserIntegration(),
    ZoteroIntegration(), MCPIntegration()]}


def describe(forge: "Forge", project_id: str) -> list[dict]:
    p = forge.projects.get(project_id)
    rows = {r["type"]: r for r in forge.projects.integrations(p["id"])}
    out = []
    for typ, integ in REGISTRY.items():
        row = rows.get(typ, {"enabled": False, "config": {}})
        entry = {"type": typ, "name": integ.name, "description": integ.description, "enabled": row["enabled"],
                 "config": row["config"], "config_fields": integ.config_fields, "actions": integ.actions}
        if row["enabled"]:
            try:
                entry["status"] = integ.status(forge, p, row["config"])
            except Exception as e:  # noqa: BLE001
                entry["status"] = {"ok": False, "detail": f"status error: {e}"}
        out.append(entry)
    return out


def mcp_config_for(forge: "Forge", project_id: str) -> Path | None:
    for r in forge.projects.integrations(project_id):
        if r["type"] == "mcp" and r["enabled"] and r["config"].get("config_path"):
            f = Path(r["config"]["config_path"]).expanduser()
            if f.is_file():
                return f
    return None
