"""Deterministic mock provider.

Used to prove the orchestration engine without spending real model usage.
Everything it produces is labelled as mock in events, runs and the UI.

Behaviour is scripted through task metadata `mock`:
  files:        {path: content} written by the implementer
  fix_files:    {path: content} written by the fixer
  reviews:      ["request_changes", "approve", ...] verdict per review cycle
  fail:         {role: n} fail the first n attempts of that role
  sleep_s:      seconds to sleep before finishing (cancellation tests)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

from .base import (ExecRequest, ExecResult, ModelInfo, ProbeResult, ProviderAdapter, ProviderStatus, Usage)

MODELS = {
    "mock-fast": {"tier": "fast"},
    "mock-standard": {"tier": "standard"},
    "mock-strong": {"tier": "strong"},
}


class MockAdapter(ProviderAdapter):
    name = "mock"
    display_name = "Mock agent (deterministic, no model)"
    is_mock = True

    def detect(self) -> ProviderStatus:
        return ProviderStatus(provider=self.name, display_name=self.display_name, installed=True, path="(builtin)",
                              version="1", auth_state="ok", auth_detail="no authentication required",
                              billing="none", is_mock=True,
                              capabilities={"model_discovery": True, "structured_output": True,
                                            "reasoning_effort": ["low", "medium", "high"], "session_resume": True,
                                            "turn_limit": "n/a"},
                              notes=["Deterministic scripted agent for testing. Not a real model."])

    def discover_models(self) -> list[ModelInfo]:
        return [ModelInfo(m, m, "discovered", {"reasoning_levels": ["low", "medium", "high"], "default_effort": "low",
                                               **v}) for m, v in MODELS.items()]

    def supported_efforts(self, model) -> list[str]:
        return ["low", "medium", "high"]

    async def probe(self, model_id: str) -> ProbeResult:
        if model_id in MODELS:
            return ProbeResult(True, model_id, "mock model always available")
        return ProbeResult(False, None, "unknown mock model", "model_unavailable")

    async def execute(self, req: ExecRequest, on_event, cancel: asyncio.Event,
                      on_start: Callable[[int], None] | None = None) -> ExecResult:
        t0 = time.monotonic()
        scen = req.metadata.get("mock") or {}
        attempt = int(req.metadata.get("attempt", 1))
        cycle = int(req.metadata.get("review_cycle", 0))

        async def emit(t: str, m: str, d: dict | None = None) -> None:
            r = on_event(t, f"[mock] {m}", {**(d or {}), "mock": True})
            if asyncio.iscoroutine(r):
                await r

        await emit("agent.session", f"mock session for role {req.role} (model {req.model})",
                   {"session_id": f"mock-{req.role}-{attempt}"})
        sleep_s = float(scen.get("sleep_s", 0))
        end = time.monotonic() + sleep_s
        while time.monotonic() < end:
            if cancel.is_set():
                return ExecResult(ok=False, error="cancelled", error_kind="cancelled",
                                  duration_s=time.monotonic() - t0, usage=Usage(billing="none"))
            if time.monotonic() - t0 > req.timeout_s:
                return ExecResult(ok=False, error=f"timed out after {req.timeout_s}s", error_kind="timeout",
                                  duration_s=time.monotonic() - t0, usage=Usage(billing="none"))
            await asyncio.sleep(0.05)

        limit_n = (scen.get("limit") or {}).get(req.role, 0)
        if attempt + (1 if req.metadata.get("continuation") else 0) <= limit_n or (scen.get("limit_always") or {}).get(req.role):
            # Reproduces a deterministic step-limit hit: partial files are written, then the run is cut off.
            for rel, content in (scen.get("partial_files") or {}).items():
                if req.sandbox == "workspace_write":
                    (req.cwd / rel).parent.mkdir(parents=True, exist_ok=True)
                    (req.cwd / rel).write_text(content)
            await emit("agent.error", f"step limit {req.max_turns} reached")
            return ExecResult(ok=False, error=f"turn limit {req.max_turns} exceeded", error_kind="turn_limit",
                              session_id=None if scen.get("no_session") else f"mock-session-{req.role}",
                              duration_s=time.monotonic() - t0,
                              usage=Usage(billing="none", turns=(req.max_turns or 0) + 1))
        fails = (scen.get("fail") or {}).get(req.role, 0)
        if attempt <= fails:
            await emit("agent.error", f"scripted failure for {req.role} attempt {attempt}")
            return ExecResult(ok=False, error=f"mock scripted failure ({req.role} attempt {attempt})",
                              error_kind="other", duration_s=time.monotonic() - t0, usage=Usage(billing="none"))

        structured: dict
        if req.role == "planner":
            structured = {
                "summary": f"Mock plan for: {req.metadata.get('objective', '')[:200]}",
                "steps": ["Inspect relevant files", "Implement the change", "Add or update tests", "Run tests"],
                "files_to_change": sorted((scen.get("files") or {}).keys()),
                "acceptance_criteria": req.metadata.get("acceptance_criteria") or ["Tests pass"],
                "test_plan": "Run the project test command.",
                "risks": [],
                "subtasks": scen.get("subtasks", []),
            }
        elif req.role in ("implementer", "fixer", "builder", "experimenter"):
            files = scen.get("fix_files" if req.role == "fixer" else "files") or {}
            if req.sandbox != "workspace_write" and files:
                return ExecResult(ok=False, error="mock implementer cannot write in a read-only sandbox",
                                  error_kind="other", duration_s=time.monotonic() - t0, usage=Usage(billing="none"))
            for rel, content in files.items():
                target = (req.cwd / rel).resolve()
                if not target.is_relative_to(req.cwd.resolve()):
                    raise ValueError("mock attempted to write outside its worktree")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                await emit("agent.tool", f"Write {rel}", {"tool": "Write"})
            structured = {"summary": f"Mock {req.role} wrote {len(files)} file(s)", "files_changed": sorted(files),
                          "tests_added": [f for f in files if "test" in f], "notes_for_reviewer": "",
                          "open_issues": []}
        elif req.role == "reviewer":
            reviews = scen.get("reviews") or ["approve"]
            verdict = reviews[min(cycle, len(reviews) - 1)]
            issues = [] if verdict == "approve" else [{
                "severity": "major", "file": next(iter(scen.get("files") or {"(unknown)": ""})),
                "description": "Mock reviewer: missing edge-case handling", "suggested_fix": "Handle the edge case"}]
            structured = {"verdict": verdict, "summary": f"Mock review cycle {cycle}: {verdict}", "issues": issues}
        elif req.role in ("librarian", "investigator"):
            ev = [{"claim": f"{req.role} claim from {req.model}", "source": "README.md", "locator": "line 1",
                   "evidence_type": "verified_in_source"},
                  {"claim": "an unverifiable claim", "source": "missing/nowhere.md", "locator": "§1",
                   "evidence_type": "source_reported"}]
            structured = {"summary": f"Mock {req.role} report by {req.model}"
                                     f"{' (resumed ' + req.resume_session_id + ')' if req.resume_session_id else ''}",
                          "claims": ev, "open_questions": ["q"], "limitations": ["mock"], "confidence": "low"}
        elif req.role == "assessor":
            structured = {"summary": "Mock novelty assessment", "candidates": [{
                "contribution": "joint transfer/reconstruct/defer", "closest_prior_art": ["R4-05"],
                "overlap": "partial", "assessment": "unknown",
                "evidence": [{"claim": "c", "source": "README.md", "locator": "1", "evidence_type": "interpretation"}]}],
                "confidence": "low"}
        elif req.role == "cross_reviewer":
            structured = {"summary": f"Mock cross-review by {req.model}", "agreements": ["a"],
                          "disagreements": [{"topic": "t", "this_position": "x", "other_position": "y",
                                             "primary_source_check": "README.md line 1", "resolution": "unresolved"}],
                          "unsupported_claims_found": ["an unverifiable claim"]}
        elif req.role == "decider":
            structured = {"decision": "INCONCLUSIVE", "justification": "Mock: evidence insufficient",
                          "evidence": [{"claim": "c", "source": "README.md", "locator": "1",
                                        "evidence_type": "verified_in_source"}],
                          "disagreements_resolved": [], "open_questions": ["full text needed"],
                          "next_steps": ["extract full text"], "confidence": "low"}
        elif req.role == "proposer":
            structured = {"title": f"Mock proposal from {req.model}", "summary": "Deterministic mock proposal",
                          "rationale": "Generated by the mock provider for testing.",
                          "steps": ["Step A", "Step B"], "evidence": ["README.md"], "risks": ["none (mock)"],
                          "success_criteria": ["Criterion 1"], "estimated_effort": "1 day"}
        else:
            structured = {"summary": f"Mock {req.role} report", "findings": ["finding"], "evidence": [],
                          "next_steps": ["next"], "confidence": "low"}
        import json
        text = json.dumps(structured) + (f"\nresumed={req.resume_session_id}" if req.resume_session_id else "")
        if req.role in ("implementer", "fixer", "builder", "experimenter"):
            text += "\nseen=" + ",".join(sorted(p.name for p in req.cwd.rglob("*") if p.is_file()
                                                 and ".git" not in p.parts))
        await emit("agent.message", f"{req.role} finished")
        return ExecResult(ok=True, text=text, structured=structured, session_id=f"mock-{req.role}-{attempt}",
                          resolved_model=req.model, usage=Usage(billing="none", turns=1),
                          exit_code=0, duration_s=time.monotonic() - t0)
