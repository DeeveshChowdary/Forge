"""Provider adapter tests against fake CLI binaries that emit the real output formats."""

import asyncio
import json
import os
import stat
import time
from pathlib import Path

import pytest

from forge.providers.base import ExecRequest
from forge.providers.claude_code import ClaudeCodeAdapter
from forge.providers.codex import CodexAdapter
from forge.providers.util import classify_error, extract_json
from forge.schemas import REVIEW


def fake_bin(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def jl(*events) -> str:
    return "".join(f"echo '{json.dumps(e)}'\n" for e in events)


REVIEW_OUT = {"verdict": "approve", "summary": "ok", "issues": []}


async def collect(adapter, req, cancel=None):
    events = []
    res = await adapter.execute(req, lambda t, m, d: events.append((t, m)), cancel or asyncio.Event())
    return res, events


async def test_claude_stream_parsing_and_structured_output(tmp_path):
    body = "cat > /dev/null\n" + jl(
        {"type": "system", "subtype": "init", "session_id": "s-1", "model": "claude-test-1", "tools": ["Read"]},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking"},
                                                       {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(REVIEW_OUT),
         "session_id": "s-1", "total_cost_usd": 0.0123, "num_turns": 2,
         "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
         "modelUsage": {"claude-test-1-20260101": {}}, "structured_output": REVIEW_OUT})
    a = ClaudeCodeAdapter(fake_bin(tmp_path, "claude", body))
    res, events = await collect(a, ExecRequest(prompt="p", cwd=tmp_path, role="reviewer", output_schema=REVIEW))
    assert res.ok and res.structured == REVIEW_OUT
    assert res.session_id == "s-1" and res.resolved_model == "claude-test-1-20260101"
    assert res.usage.cost_usd == 0.0123 and res.usage.cost_kind == "subscription_equivalent"
    assert res.usage.billing == "subscription"
    assert ("agent.tool", "Read a.py") in events


async def test_claude_auth_error_classified(tmp_path):
    body = "cat > /dev/null\n" + jl({"type": "result", "is_error": True,
                                      "result": "Failed to authenticate. API Error: 401 OAuth access token has expired"})
    a = ClaudeCodeAdapter(fake_bin(tmp_path, "claude", body))
    res, _ = await collect(a, ExecRequest(prompt="p", cwd=tmp_path, role="planner"))
    assert not res.ok and res.error_kind == "auth"


async def test_claude_turn_limit_enforced(tmp_path):
    turns = "".join(f"echo '{json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': str(i)}]}})}'; sleep 0.05\n"
                    for i in range(20))
    a = ClaudeCodeAdapter(fake_bin(tmp_path, "claude", "cat > /dev/null\n" + turns))
    res, _ = await collect(a, ExecRequest(prompt="p", cwd=tmp_path, role="implementer", max_turns=3))
    assert not res.ok and res.error_kind == "turn_limit"


async def test_claude_argv_read_only_vs_write(tmp_path):
    a = ClaudeCodeAdapter(fake_bin(tmp_path, "claude", "true"))
    ro = a.build_argv(ExecRequest(prompt="p", cwd=tmp_path, role="reviewer", model="opus", effort="high"))
    i = ro.index("--tools")
    assert ro[i + 1] == "Read,Grep,Glob"
    assert "--permission-mode" not in ro
    assert {"Edit", "Write", "Bash"} <= set(ro[ro.index("--disallowedTools") + 1:])
    assert ro[ro.index("--setting-sources") + 1] == "user" and "--strict-mcp-config" in ro
    assert ro[ro.index("--effort") + 1] == "high"
    rw = a.build_argv(ExecRequest(prompt="p", cwd=tmp_path, role="implementer", sandbox="workspace_write",
                                  allow_shell=True, shell_allowlist=["python -m pytest"]))
    assert rw[rw.index("--permission-mode") + 1] == "acceptEdits"
    assert "Bash(python -m pytest:*)" in rw
    assert "Bash" not in rw[rw.index("--allowedTools") + 1:rw.index("--allowedTools") + 8], "no unrestricted shell"


def test_clean_env_strips_api_keys_and_host_session(monkeypatch):
    from forge.providers.proc import clean_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = clean_env()
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env and "CLAUDECODE" not in env


async def test_codex_jsonl_parsing(tmp_path):
    body = "cat > /dev/null\n" + jl(
        {"type": "thread.started", "thread_id": "th-9"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 0,
                                            "aggregated_output": "1 passed"}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(REVIEW_OUT)}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 7,
                                             "reasoning_output_tokens": 2}})
    a = CodexAdapter(fake_bin(tmp_path, "codex", body))
    req = ExecRequest(prompt="p", cwd=tmp_path, role="reviewer", model="gpt-x", output_schema=REVIEW,
                      log_path=tmp_path / "run.log")
    res, events = await collect(a, req)
    assert res.ok and res.structured == REVIEW_OUT and res.session_id == "th-9"
    assert res.usage.input_tokens == 100 and res.usage.cost_usd is None and res.usage.cost_kind == "unknown"
    assert any(t == "agent.tool" and "pytest" in m for t, m in events)
    assert (tmp_path / "run.schema.json").exists()
    argv = a.build_argv(req, tmp_path / "s.json", None)
    assert argv[argv.index("-s") + 1] == "read-only" and "--ignore-user-config" in argv


async def test_codex_failure_and_resume_argv(tmp_path):
    body = "cat > /dev/null\n" + jl({"type": "turn.failed", "error": {"message": "429 Too Many Requests: usage limit"}}) + "exit 1\n"
    a = CodexAdapter(fake_bin(tmp_path, "codex", body))
    res, _ = await collect(a, ExecRequest(prompt="p", cwd=tmp_path, role="implementer"))
    assert not res.ok and res.error_kind == "rate_limit"
    argv = a.build_argv(ExecRequest(prompt="p", cwd=tmp_path, role="fixer", sandbox="workspace_write",
                                    resume_session_id="th-9", model="m"), None, None)
    assert argv[argv.index("resume") + 1:][-2:] == ["th-9", "-"]


async def test_timeout_and_cancellation_kill_process_tree(tmp_path):
    marker = tmp_path / "child.pid"
    body = f"cat > /dev/null\nsleep 30 & echo $! > {marker}\nwait\n"
    a = CodexAdapter(fake_bin(tmp_path, "codex", body))
    t0 = time.monotonic()
    res, _ = await collect(a, ExecRequest(prompt="p", cwd=tmp_path, role="x", timeout_s=1))
    assert res.error_kind == "timeout" and time.monotonic() - t0 < 10
    child = int(marker.read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(0.5, cancel.set)
    res, _ = await collect(a, ExecRequest(prompt="p", cwd=tmp_path, role="x", timeout_s=30), cancel)
    assert res.error_kind == "cancelled"


def test_codex_discovery_from_catalog(tmp_path):
    catalog = {"models": [
        {"slug": "m-1", "display_name": "M1", "visibility": "list", "priority": 1, "default_reasoning_level": "low",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "m-hidden", "display_name": "H", "visibility": "hide", "supported_reasoning_levels": []}]}
    body = f"if [ \"$1\" = debug ]; then echo '{json.dumps(catalog)}'; fi\n"
    a = CodexAdapter(fake_bin(tmp_path, "codex", body))
    ms = a.discover_models()
    assert [(m.model_id, m.hidden) for m in ms] == [("m-1", False), ("m-hidden", True)]
    assert ms[0].capabilities["reasoning_levels"] == ["low", "high"]


def test_util_helpers():
    assert extract_json('noise ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('prefix {"b": 2} suffix') == {"b": 2}
    assert extract_json("nothing") is None
    assert classify_error("model not found: foo") == "model_unavailable"
    assert classify_error("weird") == "other"


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("FORGE_LIVE_TESTS") != "1", reason="set FORGE_LIVE_TESTS=1 to probe real CLIs")
async def test_live_probes():
    for a, model in ((CodexAdapter(), "gpt-6.1-sol"), (ClaudeCodeAdapter(), "sonnet")):
        st = a.detect()
        if not st.installed:
            pytest.skip(f"{a.name} not installed")
        r = await a.probe(model)
        print(a.name, model, r)
