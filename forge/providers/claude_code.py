"""Anthropic Claude Code CLI adapter (`claude -p`).

Verified against Claude Code 2.1.x `--help`:
  -p/--print, --output-format stream-json (+ --verbose), --model, --effort
  (low|medium|high|max), --json-schema, --tools, --allowedTools,
  --disallowedTools, --permission-mode, --append-system-prompt, --resume,
  --add-dir, --setting-sources, --mcp-config, --strict-mcp-config.

The CLI has no model-listing command, so models come from configured entries
(aliases such as "sonnet"/"opus" or full ids) and are confirmed by a probe; the
probe also records the exact model id the CLI resolved the alias to.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .base import (ExecRequest, ExecResult, ModelInfo, ProbeResult, ProviderAdapter, ProviderStatus, Usage)
from .proc import Aborted, clean_env, run_simple, run_streaming
from .util import classify_error, extract_json, shorten

EFFORTS = ["low", "medium", "high", "max"]
READ_TOOLS = ["Read", "Grep", "Glob"]
# Claude Code's supported `attribution` setting: empty strings hide commit/PR attribution.
NO_ATTRIBUTION = json.dumps({"attribution": {"commit": "", "pr": ""}})
WRITE_TOOLS = ["Edit", "Write"]
NET_TOOLS = ["WebSearch", "WebFetch"]


class ClaudeCodeAdapter(ProviderAdapter):
    name = "claude_code"
    display_name = "Claude Code CLI (Anthropic)"

    def __init__(self, path_override: str = "", configured_models: list[str] | None = None,
                 disable_attribution: bool = True):
        self.path_override = path_override
        self.configured_models = configured_models or ["sonnet", "opus", "haiku"]
        self.disable_attribution = disable_attribution

    def binary(self) -> str | None:
        if self.path_override and Path(self.path_override).exists():
            return self.path_override
        for cand in (shutil.which("claude"), str(Path.home() / ".local/bin/claude"),
                     str(Path.home() / ".claude/local/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
            if cand and Path(cand).exists():
                return cand
        return None

    def env(self) -> dict[str, str]:
        # No ANTHROPIC_API_KEY / host-session variables: the CLI uses its own stored login.
        return clean_env()

    def detect(self) -> ProviderStatus:
        b = self.binary()
        st = ProviderStatus(provider=self.name, display_name=self.display_name, installed=bool(b), path=b)
        if not b:
            st.auth_state = "missing"
            st.notes.append("claude CLI not found on PATH or common install locations")
            return st
        env = self.env()
        try:
            st.version = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20,
                                        env=env).stdout.strip()
        except Exception as e:  # noqa: BLE001
            st.notes.append(f"version check failed: {e}")
        try:
            p = subprocess.run([b, "auth", "status"], capture_output=True, text=True, timeout=20, env=env)
            data = json.loads(p.stdout or "{}")
            if data.get("loggedIn"):
                # Logged in according to local state; only a probe proves the credentials work.
                st.auth_state = "unverified"
                method = data.get("authMethod", "")
                sub = data.get("subscriptionType") or ""
                st.billing = "subscription" if method in ("claude.ai", "oauth") else (
                    "api" if "api" in method.lower() else "unknown")
                st.auth_detail = f"logged in via {method}" + (f" ({sub} plan)" if sub else "")
            else:
                st.auth_state = "missing"
                st.auth_detail = "not logged in (run `claude` and use /login)"
        except Exception as e:  # noqa: BLE001
            st.auth_detail = f"auth status unavailable: {e}"
        st.capabilities = {
            "model_discovery": False,
            "structured_output": "--json-schema",
            "reasoning_effort": EFFORTS,
            "session_resume": True,
            "streaming": "stream-json",
            "tool_restriction": True,
            "sandbox": "tool allowlist + permission-mode (no OS sandbox)",
            "turn_limit": "enforced by Forge (stream monitoring)",
            "cost_reporting": "total_cost_usd (list-price estimate; not billed on subscription plans)",
        }
        return st

    def discover_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id=m, display_name=f"Claude {m}" if "-" not in m else m, source="configured",
                          capabilities={"reasoning_levels": EFFORTS, "default_effort": "medium"})
                for m in self.configured_models]

    def supported_efforts(self, model: Any) -> list[str]:
        return EFFORTS

    async def probe(self, model_id: str) -> ProbeResult:
        b = self.binary()
        if not b:
            return ProbeResult(False, None, "claude CLI not installed", "other")
        argv = [b, "-p", "--output-format", "json", "--model", model_id, "--tools", "",
                "--no-session-persistence", "--setting-sources", "user", "--strict-mcp-config"]
        tmp = Path(os.environ.get("TMPDIR", "/tmp"))
        code, out, err = await run_simple(argv + ["Reply with exactly: PONG"], cwd=tmp, env=self.env(), timeout_s=120)
        try:
            data = json.loads(out)
        except ValueError:
            return ProbeResult(False, None, shorten(err or out or "no output"), classify_error(err or out))
        if data.get("is_error"):
            msg = str(data.get("result", ""))
            return ProbeResult(False, None, shorten(msg), classify_error(msg))
        resolved = next(iter((data.get("modelUsage") or {}).keys()), None)
        return ProbeResult(True, resolved, f"probe ok: {shorten(str(data.get('result','')), 60)}")

    def build_argv(self, req: ExecRequest) -> list[str]:
        b = self.binary()
        assert b
        argv = [b, "-p", "--output-format", "stream-json", "--verbose",
                "--setting-sources", "user",      # ignore repository-provided .claude settings (untrusted)
                "--strict-mcp-config"]            # ignore repository .mcp.json unless Forge passes one
        if self.disable_attribution:
            argv += ["--settings", NO_ATTRIBUTION]  # no AI co-author trailers in any commit/PR the agent makes
        if req.mcp_config_path:
            argv += ["--mcp-config", str(req.mcp_config_path)]
        if req.model:
            argv += ["--model", req.model]
        if req.effort in EFFORTS:
            argv += ["--effort", req.effort]
        tools = list(READ_TOOLS)
        allowed = list(READ_TOOLS)
        if req.sandbox == "workspace_write":
            tools += WRITE_TOOLS
            allowed += WRITE_TOOLS
            argv += ["--permission-mode", "acceptEdits"]
        if req.allow_shell and req.shell_allowlist:
            tools.append("Bash")
            allowed += [f"Bash({p}:*)" for p in req.shell_allowlist]
        if req.allow_network:
            tools += NET_TOOLS
            allowed += NET_TOOLS
        argv += ["--tools", ",".join(tools), "--allowedTools", *allowed]
        disallowed = [t for t in ("Edit", "Write", "NotebookEdit", "Bash", "WebSearch", "WebFetch")
                      if t not in tools]
        if disallowed:
            argv += ["--disallowedTools", *disallowed]
        for d in req.extra_readable_dirs:
            argv += ["--add-dir", str(d)]
        if req.output_schema:
            argv += ["--json-schema", json.dumps(req.output_schema)]
        if req.system_append:
            argv += ["--append-system-prompt", req.system_append]
        if req.resume_session_id:
            argv += ["--resume", req.resume_session_id]
        return argv

    async def execute(self, req: ExecRequest, on_event, cancel: asyncio.Event,
                      on_start: Callable[[int], None] | None = None) -> ExecResult:
        if not self.binary():
            return ExecResult(ok=False, error="claude CLI not installed", error_kind="other")
        argv = self.build_argv(req)
        state: dict[str, Any] = {"turns": 0, "result": None, "session": None, "model": None, "texts": []}

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
            if typ == "system" and ev.get("subtype") == "init":
                state["session"] = ev.get("session_id")
                state["model"] = ev.get("model")
                await emit("agent.session", f"Claude session started (model {ev.get('model')})",
                           {"session_id": ev.get("session_id"), "model": ev.get("model"),
                            "tools": ev.get("tools")})
            elif typ == "assistant":
                state["turns"] += 1
                for block in (ev.get("message") or {}).get("content", []):
                    bt = block.get("type")
                    if bt == "text" and block.get("text", "").strip():
                        state["texts"].append(block["text"])
                        await emit("agent.message", shorten(block["text"], 2000))
                    elif bt == "tool_use":
                        inp = block.get("input") or {}
                        target = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("path") or ""
                        await emit("agent.tool", f"{block.get('name')} {shorten(str(target), 200)}",
                                   {"tool": block.get("name")})
                    elif bt == "thinking":
                        await emit("agent.thinking", "(thinking)")
                if req.max_turns and state["turns"] > req.max_turns:
                    raise Aborted(f"turn limit {req.max_turns} exceeded")
            elif typ == "result":
                state["result"] = ev

        outcome = await run_streaming(argv, cwd=req.cwd, env=self.env(), stdin_text=req.prompt,
                                      timeout_s=req.timeout_s, cancel=cancel, on_line=on_line,
                                      log_path=req.log_path, on_start=on_start)
        res: dict = state["result"] or {}
        usage_raw = res.get("usage") or {}
        model_usage = res.get("modelUsage") or {}
        resolved = next(iter(model_usage.keys()), None) or state["model"]
        cost = res.get("total_cost_usd")
        usage = Usage(
            input_tokens=(usage_raw.get("input_tokens") or 0) + (usage_raw.get("cache_creation_input_tokens") or 0)
            if usage_raw else None,
            output_tokens=usage_raw.get("output_tokens") if usage_raw else None,
            cached_input_tokens=usage_raw.get("cache_read_input_tokens") if usage_raw else None,
            cost_usd=cost,
            cost_kind="subscription_equivalent" if cost is not None else "unknown",
            billing="subscription",
            turns=res.get("num_turns") or state["turns"],
        )
        text = res.get("result") if isinstance(res.get("result"), str) else "\n".join(state["texts"])
        structured = res.get("structured_output")
        if structured is None and req.output_schema:
            structured = extract_json(text or "")
        result = ExecResult(ok=False, text=text or "", structured=structured, session_id=res.get("session_id") or state["session"],
                            resolved_model=resolved, usage=usage, exit_code=outcome.exit_code,
                            duration_s=outcome.duration_s, permission_denials=res.get("permission_denials") or [])
        if outcome.cancelled:
            result.error, result.error_kind = "cancelled", "cancelled"
        elif outcome.timed_out:
            result.error, result.error_kind = f"timed out after {req.timeout_s}s", "timeout"
        elif outcome.aborted_reason:
            result.error, result.error_kind = outcome.aborted_reason, "turn_limit"
        elif not res:
            msg = outcome.stderr or "no result event from claude"
            result.error, result.error_kind = shorten(msg, 1000), classify_error(msg)
        elif res.get("is_error"):
            msg = str(res.get("result") or res.get("subtype"))
            result.error, result.error_kind = shorten(msg, 1000), classify_error(msg)
        elif req.output_schema and structured is None:
            result.error, result.error_kind = "model did not return the required structured output", "bad_output"
        else:
            result.ok = True
        return result
