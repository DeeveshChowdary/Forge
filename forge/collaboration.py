"""Multi-model collaboration policies.

Policies are enforced in the engine through hard router constraints
(`require_providers` / `forbid_providers`). If a policy cannot be satisfied the
phase is BLOCKED with the reason — it never silently falls back to one provider.

* auto                 router chooses freely
* cross_model          at least two distinct providers participate; reviewers never use the
                       writer's provider; fan-out agents use distinct providers
* independent_research fan-out phases run Claude and Codex separately (exactly those two)
* implement_review     review phases must use a different provider than implementation phases
* manual               every agent phase needs an explicit provider/model (routing_overrides)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .workflows import Phase, Workflow

if TYPE_CHECKING:
    from .app import Forge

POLICIES = ("auto", "cross_model", "independent_research", "implement_review", "manual")
INDEPENDENT_PROVIDERS = ("claude_code", "codex")
WRITER_ROLES = {"implementer", "fixer", "builder", "experimenter"}
REVIEW_ROLES = {"reviewer"}


class PolicyError(Exception):
    pass


@dataclass
class Constraint:
    require: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    reason: str = ""


def agent_phases(wf: Workflow) -> list[Phase]:
    return [p for p in wf.phases if p.kind in ("agent", "fanout")]


def fanout_count(policy: str, metadata: dict) -> int:
    if policy == "independent_research":
        return len(INDEPENDENT_PROVIDERS)
    return int(metadata.get("fanout_count", 2))


def constraint_for(policy: str, wf: Workflow, phase: Phase, *, used_by_role: dict[str, set[str]],
                   used_all: set[str], fanout_index: int | None = None,
                   fanout_used: list[str] | None = None) -> Constraint:
    c = Constraint()
    writers = set().union(*[v for r, v in used_by_role.items() if r in WRITER_ROLES]) if used_by_role else set()
    if policy in ("implement_review", "cross_model") and phase.role in REVIEW_ROLES and writers:
        c.forbid += sorted(writers)
        c.reason = f"{policy}: reviewer must differ from implementer provider(s) {sorted(writers)}"
    if phase.kind == "fanout" and fanout_index is not None:
        if policy == "independent_research":
            c.require = [INDEPENDENT_PROVIDERS[fanout_index % len(INDEPENDENT_PROVIDERS)]]
            c.reason = f"independent_research: agent #{fanout_index + 1} must be {c.require[0]}"
        elif policy == "cross_model" and fanout_used:
            c.forbid += [p for p in fanout_used if p not in c.forbid]
            c.reason = "cross_model: fan-out agents must use distinct providers"
    if policy == "cross_model" and phase.kind == "agent" and not c.forbid:
        later = agent_phases(wf)
        is_last = later and later[-1].key == phase.key
        if is_last and len(used_all) == 1:
            c.forbid = sorted(used_all)
            c.reason = f"cross_model: last agent phase must add a second provider (only {sorted(used_all)} so far)"
    return c


def validate_submission(policy: str, wf: Workflow, overrides: dict) -> None:
    if policy not in POLICIES:
        raise PolicyError(f"unknown collaboration policy {policy!r}; expected one of {POLICIES}")
    if policy == "manual":
        missing = [p.key for p in agent_phases(wf) if not overrides.get(p.key)]
        if missing:
            raise PolicyError(f"manual collaboration requires provider/model for phases: {missing}")


def plan_routes(forge: "Forge", project_id: str, wf: Workflow, *, policy: str, mode: str,
                complexity: str = "medium", criticality: str = "normal", overrides: dict | None = None,
                metadata: dict | None = None) -> dict[str, Any]:
    """Dry-run routing for every agent phase under the policy (nothing is persisted)."""
    from .router import RouteContext, RoutingError
    overrides = overrides or {}
    metadata = metadata or {}
    used_by_role: dict[str, set[str]] = {}
    used_all: set[str] = set()
    phases, blockers = [], []
    for ph in wf.phases:
        if ph.kind not in ("agent", "fanout"):
            phases.append({"phase": ph.key, "kind": ph.kind, "role": ph.role, "agents": []})
            continue
        n = fanout_count(policy, metadata) if ph.kind == "fanout" else 1
        ov = overrides.get(ph.key)
        agents, f_used = [], []
        for i in range(n):
            o = (ov[i] if isinstance(ov, list) and i < len(ov) else (ov if isinstance(ov, dict) else None))
            c = constraint_for(policy, wf, ph, used_by_role=used_by_role, used_all=used_all,
                               fanout_index=i if ph.kind == "fanout" else None, fanout_used=f_used)
            try:
                if policy == "manual" and not o:
                    raise RoutingError(f"manual policy: no provider/model given for {ph.key}")
                d = forge.router.route(RouteContext(
                    project_id=project_id, task_id="plan", phase=ph.key, role=ph.role, complexity=complexity,
                    criticality=criticality, mode="manual" if policy == "manual" else mode, override=o,
                    require_providers=c.require, forbid_providers=c.forbid,
                    exclude=[(a["provider"], a["model"]) for a in agents]), persist=False)
                agents.append({"provider": d.provider, "model": d.model, "effort": d.effort,
                               "policy_reason": c.reason, "reasons": d.reasons})
                f_used.append(d.provider)
                used_by_role.setdefault(ph.role, set()).add(d.provider)
                used_all.add(d.provider)
            except RoutingError as e:
                agents.append({"error": str(e), "policy_reason": c.reason})
                blockers.append(f"{ph.key}{f' #{i + 1}' if n > 1 else ''}: {e}")
        phases.append({"phase": ph.key, "kind": ph.kind, "role": ph.role, "max_turns": ph.max_turns,
                       "agents": agents})
    if policy == "cross_model" and len(used_all) < 2 and not blockers:
        blockers.append(f"cross_model: only {sorted(used_all)} would participate")
    return {"policy": policy, "phases": phases, "providers": sorted(used_all), "blockers": blockers,
            "ok": not blockers}
