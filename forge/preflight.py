"""Preflight checks run before a task is queued.

Heuristic, explainable estimates — they are warnings, not predictions. They are
designed to catch the failure mode seen in task t_fa827e885e2d: a large,
multi-section literature objective sent to a single code-experiment phase with a
40-step limit and no multi-model policy.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import collaboration
from .workflows import Workflow

if TYPE_CHECKING:
    from .app import Forge

_SECTION = re.compile(r"(?m)^\s{0,3}#{2,3}\s+\S")
_PATH = re.compile(r"(?<![\w/])[\w.-]+(?:/[\w.-]+)+")
_LIT = re.compile(r"\b(literature|prior[- ]art|novelty|survey|related work|citation|bibliograph|paper)s?\b", re.I)
_CODE = re.compile(r"\b(implement|benchmark|simulat\w*|prototype|unit tests?|code|script|run the experiment)\b", re.I)
_ENV = re.compile(r"\b(verify (?:that )?\w+ (?:is|are) (?:available|authenticated|installed)|authenticated|install|"
                  r"dependenc(?:y|ies)|zotero access|environment)\b", re.I)


def split_sections(objective: str) -> list[tuple[str, str]]:
    """Top-level markdown sections (## or ###) as (title, body)."""
    parts = re.split(r"(?m)^\s{0,3}#{2,3}\s+(.+)$", objective)
    out = []
    for i in range(1, len(parts) - 1, 2):
        title, body = parts[i].strip(), parts[i + 1].strip()
        if body:
            out.append((title, body))
    return out


def estimate_steps(objective: str, *, writes: bool) -> int:
    """Rough tool-step estimate for one phase. Phases that produce the deliverables (writes) do most of the
    reading and writing, so they get the larger share; it is deliberately conservative."""
    sections = len(_SECTION.findall(objective))
    paths = len(set(_PATH.findall(objective)))
    env = len(_ENV.findall(objective))
    total = 12 + 3 * paths + 8 * sections + 4 * env + len(objective) // 400
    return max(8, int(total * (0.7 if writes else 0.35)))


def phase_limit(wf: Workflow, phase_key: str, budget: dict) -> int:
    ph = wf.phase(phase_key)
    override = ((budget or {}).get("phase_limits") or {}).get(phase_key, {})
    return int(override.get("max_turns") or ph.max_turns or (budget or {}).get("max_turns", 40))


def run(forge: "Forge", project_id: str, wf: Workflow, objective: str, *, policy: str, mode: str,
        complexity: str = "medium", overrides: dict | None = None, budget: dict | None = None,
        metadata: dict | None = None) -> dict[str, Any]:
    warnings: list[str] = []
    budget = budget or {}
    agent = collaboration.agent_phases(wf)
    estimates = {}
    for ph in agent:
        est = estimate_steps(objective, writes=ph.writes or ph.report)
        lim = phase_limit(wf, ph.key, budget)
        estimates[ph.key] = {"estimated_steps": est, "limit": lim}
        if est > lim:
            warnings.append(f"Phase '{ph.key}': estimated ~{est} agent steps exceeds its limit of {lim}. "
                            "Raise the phase limit, narrow the objective, or decompose it.")
    sections = split_sections(objective)
    if len(sections) >= 4 and len(objective) > 3000:
        warnings.append(f"The objective has {len(sections)} sections ({len(objective)} characters) for "
                        f"{len(agent)} agent phase(s); consider decomposing it into dependent subtasks "
                        "(decompose='sections').")
    lit, code = len(_LIT.findall(objective)), len(_CODE.findall(objective))
    if any(p.key == "experiment" for p in wf.phases) and lit >= 3 and lit > 2 * code:
        warnings.append(f"Literature-heavy objective sent to '{wf.key}', whose 'experiment' phase expects an "
                        "executable experiment. Use the 'literature_review' workflow.")
    if re.search(r"\bclaude\b", objective, re.I) and re.search(r"\bcodex\b", objective, re.I) and policy == "auto":
        warnings.append("The objective asks for both Claude and Codex, but the collaboration policy is 'auto' "
                        "(the router may use one provider for everything). Use 'independent_research' or "
                        "'cross_model' to enforce it.")
    if len(_ENV.findall(objective)) >= 2:
        warnings.append("The objective asks agents to verify tools, authentication or environment. Forge already "
                        "checks providers; these steps consume agent turns and may be impossible in read-only "
                        "phases. Remove them or run `forge doctor` instead.")
    plan = collaboration.plan_routes(forge, project_id, wf, policy=policy, mode=mode, complexity=complexity,
                                     overrides=overrides, metadata=metadata)
    return {"warnings": warnings, "estimates": estimates, "sections": [t for t, _ in sections],
            "recommend_decompose": len(sections) >= 4 and len(objective) > 3000,
            "route_plan": plan, "blockers": plan["blockers"]}
