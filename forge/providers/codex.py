"""OpenAI Codex CLI adapter (`codex exec`).

Verified against codex-cli 0.164 `--help`:
  exec --json (JSONL events), -m/--model, -c model_reasoning_effort=...,
  -s/--sandbox read-only|workspace-write, -C/--cd, --output-schema FILE,
  -o/--output-last-message FILE, --skip-git-repo-check, --ephemeral,
  --ignore-user-config, `exec resume <id> -`, `debug models` (model catalog),
  `login status`.

The binary is often bundled with the editor extension rather than on PATH, so
detection also searches the VS Code extension directories.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .base import (ExecRequest, ExecResult, ModelInfo, ProbeResult, ProviderAdapter, ProviderStatus, Usage)
from .proc import Aborted, clean_env, run_simple, run_streaming
from .util import classify_error, extract_json, shorten


def _version_key(p: str) -> tuple:
    m = re.search(r"openai\.chatgpt-(\d+)\.(\d+)\.(\d+)", p)
    return tuple(int(x) for x in m.groups()) if m else (0,)


class CodexAdapter(ProviderAdapter):
    name = "codex"
    display_name = "Codex CLI (OpenAI)"

    def __init__(self, path_override: str = "", ignore_user_config: bool = True):
        self.path_override = path_override
        self.ignore_user_config = ignore_user_config
        self._catalog: list[dict] | None = None

    def binary(self) -> str | None:
        if self.path_override and Path(self.path_override).exists():
            return self.path_override
        found = shutil.which("codex")
        if found:
            return found
        home = Path.home()
        patterns = [str(home / ".vscode/extensions/openai.chatgpt-*/bin/*/codex"),
                    str(home / ".cursor/extensions/openai.chatgpt-*/bin/*/codex"),
                    "/Applications/Codex.app/Contents/Resources/codex"]
        cands: list[str] = []
        for pat in patterns:
            cands += [c for c in glob.glob(pat) if os.access(c, os.X_OK)]
        if not cands:
            return None
        return sorted(cands, key=_version_key)[-1]

    def env(self) -> dict[str, str]:
        b = self.binary()
        prepend = []
        if b:
            prepend.append(str(Path(b).parent))
            bundled = Path(b).parent / "codex-path"  # editor bundles ship rg here
            if bundled.is_dir():
                prepend.append(str(bundled))
        return clean_env(path_prepend=prepend)  # no OPENAI_API_KEY: uses the stored ChatGPT login

    def _common(self) -> list[str]:
        return ["--ignore-user-config"] if self.ignore_user_config else []

    def detect(self) -> ProviderStatus:
        b = self.binary()
        st = ProviderStatus(provider=self.name, display_name=self.display_name, installed=bool(b), path=b)
        if not b:
            st.auth_state = "missing"
            st.notes.append("codex CLI not found on PATH or in editor extension bundles")
            return st
        env = self.env()
        try:
            st.version = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20,
                                        env=env).stdout.strip()
        except Exception as e:  # noqa: BLE001
            st.notes.append(f"version check failed: {e}")
        try:
            p = subprocess.run([b, "login", "status"], capture_output=True, text=True, timeout=20, env=env)
            out = (p.stdout + p.stderr).strip()
            if p.returncode == 0 and "logged in" in out.lower():
                st.auth_state = "unverified"
                st.auth_detail = out.splitlines()[0]
                st.billing = "subscription" if "chatgpt" in out.lower() else (
                    "api" if "api key" in out.lower() else "unknown")
            else:
                st.auth_state = "missing"
                st.auth_detail = out[:200] or "not logged in (run `codex login`)"
        except Exception as e:  # noqa: BLE001
            st.auth_detail = f"login status unavailable: {e}"
        if "extensions/openai.chatgpt" in (b or ""):
            st.notes.append("using the Codex binary bundled with the VS Code extension")
        st.capabilities = {
            "model_discovery": "codex debug models",
            "structured_output": "--output-schema",
            "reasoning_effort": "per model (catalog)",
            "session_resume": True,
            "streaming": "--json JSONL",
            "tool_restriction": "sandbox modes",
            "sandbox": "OS sandbox: read-only | workspace-write",
            "turn_limit": "enforced by Forge (stream monitoring)",
            "cost_reporting": "token counts only (no cost)",
        }
        return st

    def _load_catalog(self) -> list[dict]:
        b = self.binary()
        if not b:
            return []
        try:
            p = subprocess.run([b, "debug", "models"], capture_output=True, text=True, timeout=60, env=self.env())
            data = json.loads(p.stdout)
            self._catalog = data.get("models", data if isinstance(data, list) else [])
        except Exception:  # noqa: BLE001
            self._catalog = []
        return self._catalog

    def discover_models(self) -> list[ModelInfo]:
        out = []
        for m in self._load_catalog():
            slug = m.get("slug")
            if not slug:
                continue
            levels = [r.get("effort") for r in m.get("supported_reasoning_levels", []) if r.get("effort")]
            out.append(ModelInfo(
                model_id=slug, display_name=m.get("display_name") or slug, source="discovered",
                hidden=m.get("visibility") != "list",
                capabilities={"reasoning_levels": levels, "default_effort": m.get("default_reasoning_level"),
                              "description": m.get("description", ""), "catalog_priority": m.get("priority"),
                              "speed_tiers": m.get("additional_speed_tiers", [])},
            ))
        return out

    def supported_efforts(self, model: Any) -> list[str]:
        caps = (model or {}).get("capabilities") if isinstance(model, dict) else getattr(model, "capabilities", {})
        return list((caps or {}).get("reasoning_levels") or [])

    async def probe(self, model_id: str) -> ProbeResult:
        b = self.binary()
        if not b:
            return ProbeResult(False, None, "codex CLI not installed", "other")
        argv = [b, "exec", *self._common(), "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                "-m", model_id, "-c", "model_reasoning_effort=\"low\"", "Reply with exactly: PONG"]
        tmp = Path(os.environ.get("TMPDIR", "/tmp"))
        code, out, err = await run_simple(argv, cwd=tmp, env=self.env(), timeout_s=180)
        text, error = "", None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                text = ev["item"].get("text", "")
            elif ev.get("type") in ("turn.failed", "error"):
                error = json.dumps(ev.get("error") or ev.get("message") or ev)
        if code == 0 and text and not error:
            return ProbeResult(True, model_id, f"probe ok: {shorten(text, 60)} (codex exec accepted model id)")
        msg = error or err or out or f"exit {code}"
        return ProbeResult(False, None, shorten(msg), classify_error(msg))

    def build_argv(self, req: ExecRequest, schema_path: Path | None, last_msg_path: Path | None) -> list[str]:
        b = self.binary()
        assert b
        opts = [*self._common(), "--json", "--skip-git-repo-check", "-C", str(req.cwd),
                "-s", "workspace-write" if req.sandbox == "workspace_write" else "read-only"]
        if req.model:
            opts += ["-m", req.model]
        if req.effort:
            opts += ["-c", f'model_reasoning_effort="{req.effort}"']
        if req.allow_network and req.sandbox == "workspace_write":
            opts += ["-c", "sandbox_workspace_write.network_access=true"]
        if schema_path:
            opts += ["--output-schema", str(schema_path)]
        if last_msg_path:
            opts += ["-o", str(last_msg_path)]
        for d in req.extra_readable_dirs:
            opts += ["--add-dir", str(d)] if req.sandbox == "workspace_write" else []
        if req.resume_session_id:
            # `exec resume` accepts a narrower option set; keep config overrides only.
            keep = [*self._common()]
            if req.model:
                keep += ["-c", f'model="{req.model}"']
            if req.effort:
                keep += ["-c", f'model_reasoning_effort="{req.effort}"']
            return [b, "exec", "--json", "--skip-git-repo-check", "-C", str(req.cwd),
                    "-s", "workspace-write" if req.sandbox == "workspace_write" else "read-only",
                    *(["-o", str(last_msg_path)] if last_msg_path else []),
                    "resume", *keep, req.resume_session_id, "-"]
        return [b, "exec", *opts, "-"]

    async def execute(self, req: ExecRequest, on_event, cancel: asyncio.Event,
                      on_start: Callable[[int], None] | None = None) -> ExecResult:
        if not self.binary():
            return ExecResult(ok=False, error="codex CLI not installed", error_kind="other")
        work = (req.log_path.parent if req.log_path else Path(os.environ.get("TMPDIR", "/tmp")))
        stem = req.log_path.stem if req.log_path else "codex"
        schema_path = None
        if req.output_schema:
            schema_path = work / f"{stem}.schema.json"
            schema_path.write_text(json.dumps(req.output_schema))
        last_msg = work / f"{stem}.last.txt"
        prompt = req.prompt if not req.system_append else f"{req.system_append}\n\n---\n\n{req.prompt}"
        argv = self.build_argv(req, schema_path, last_msg)
        state: dict[str, Any] = {"steps": 0, "session": None, "usage": None, "messages": [], "error": None}

        async def emit(t: str, m: str, d: dict | None = None) -> None:
            r = on_event(t, m, d or {})
            if asyncio.iscoroutine(r):
                await r

        async def on_line(line: str) -> None:
            if not line.strip():
                return
            try:
                ev = json.loads(line)
            except ValueError:
                await emit("agent.raw", shorten(line, 300))
                return
            typ = ev.get("type")
            if typ == "thread.started":
                state["session"] = ev.get("thread_id")
                await emit("agent.session", f"Codex thread started", {"session_id": ev.get("thread_id"),
                                                                      "model": req.model})
            elif typ in ("item.started", "item.completed"):
                item = ev.get("item") or {}
                it = item.get("type")
                if typ == "item.completed" and it == "agent_message":
                    state["messages"].append(item.get("text", ""))
                    await emit("agent.message", shorten(item.get("text", ""), 2000))
                elif it == "reasoning" and typ == "item.completed":
                    await emit("agent.thinking", shorten(item.get("text", "(reasoning)"), 300))
                elif it == "command_execution":
                    if typ == "item.started":
                        state["steps"] += 1
                        await emit("agent.tool", f"$ {shorten(item.get('command', ''), 300)}", {"tool": "shell"})
                    else:
                        await emit("agent.tool_result",
                                   f"exit {item.get('exit_code')}: {shorten(item.get('aggregated_output', ''), 300)}",
                                   {"tool": "shell", "exit_code": item.get("exit_code")})
                elif it == "file_change" and typ == "item.completed":
                    state["steps"] += 1
                    changes = ", ".join(f"{c.get('kind')} {c.get('path')}" for c in item.get("changes", []))
                    await emit("agent.tool", f"edit: {shorten(changes, 300)}", {"tool": "apply_patch"})
                elif it in ("mcp_tool_call", "web_search") and typ == "item.started":
                    state["steps"] += 1
                    await emit("agent.tool", f"{it}: {shorten(json.dumps(item), 200)}", {"tool": it})
                elif it == "error":
                    await emit("agent.error", shorten(item.get("message", ""), 500))
                if req.max_turns and state["steps"] > req.max_turns:
                    raise Aborted(f"step limit {req.max_turns} exceeded")
            elif typ == "turn.completed":
                state["usage"] = ev.get("usage") or {}
            elif typ in ("turn.failed", "error"):
                err = ev.get("error") or {}
                state["error"] = err.get("message") if isinstance(err, dict) else str(err or ev.get("message"))
                await emit("agent.error", shorten(state["error"] or "error", 500))

        outcome = await run_streaming(argv, cwd=req.cwd, env=self.env(), stdin_text=prompt,
                                      timeout_s=req.timeout_s, cancel=cancel, on_line=on_line,
                                      log_path=req.log_path, on_start=on_start)
        u = state["usage"] or {}
        usage = Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                      cached_input_tokens=u.get("cached_input_tokens"), reasoning_tokens=u.get("reasoning_output_tokens"),
                      cost_usd=None, cost_kind="unknown", billing="subscription", turns=state["steps"])
        text = last_msg.read_text() if last_msg.exists() else "\n".join(state["messages"])
        structured = extract_json(text) if req.output_schema else None
        result = ExecResult(ok=False, text=text, structured=structured, session_id=state["session"],
                            resolved_model=req.model, usage=usage, exit_code=outcome.exit_code,
                            duration_s=outcome.duration_s)
        if outcome.cancelled:
            result.error, result.error_kind = "cancelled", "cancelled"
        elif outcome.timed_out:
            result.error, result.error_kind = f"timed out after {req.timeout_s}s", "timeout"
        elif outcome.aborted_reason:
            result.error, result.error_kind = outcome.aborted_reason, "turn_limit"
        elif state["error"] or outcome.exit_code != 0:
            msg = state["error"] or outcome.stderr or f"codex exited {outcome.exit_code}"
            result.error, result.error_kind = shorten(msg, 1000), classify_error(msg)
        elif req.output_schema and structured is None:
            result.error, result.error_kind = "model did not return the required structured output", "bad_output"
        else:
            result.ok = True
        return result
