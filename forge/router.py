"""Model router.

`RoutingPolicy` is the replaceable interface; `HeuristicPolicy` is the initial
explainable implementation. A future contextual-bandit policy only needs to
implement `select()` and (optionally) learn in `record_outcome()`; the
`RouteContext` already carries the features it would need.

The heuristic never claims learned knowledge: historical success and latency
are computed from Forge's own recorded runs and are shown with their sample size.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .db import dumps, loads, new_id, now
from .domain import ProjectConfig

if TYPE_CHECKING:
    from .app import Forge

TIERS = ["fast", "standard", "strong"]
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
# Efforts never chosen automatically (they change behaviour, e.g. codex "ultra" delegates sub-tasks).
AUTO_EFFORT_CAP = "xhigh"
STRONG_ROLES = {"planner", "reviewer", "designer", "analyst", "proposer", "investigator"}
MODE_WEIGHTS = {
    "balanced": {"quality": 1.0, "cost": 0.5, "latency": 0.3, "overshoot": 0.35},
    "max_quality": {"quality": 2.0, "cost": 0.0, "latency": 0.1, "overshoot": 0.0},
    "economy": {"quality": 0.6, "cost": 1.5, "latency": 0.4, "overshoot": 0.6},
}
RATE_LIMIT_COOLDOWN_S = 15 * 60


class RoutingError(Exception):
    pass


@dataclass
class RouteContext:
    project_id: str
    task_id: str
    phase: str
    role: str
    category: str = "feature"
    complexity: str = "medium"        # low | medium | high
    criticality: str = "normal"       # low | normal | high
    mode: str = "balanced"
    override: dict | None = None      # {provider, model, effort}
    avoid_providers: list[str] = field(default_factory=list)   # e.g. implementer's provider for an independent review
    independent: bool = False          # hard-exclude avoid_providers when an alternative exists
    escalation: int = 0                # raised after failures (economy escalates instead of retrying the same tier)
    exclude: list[tuple[str, str]] = field(default_factory=list)  # (provider, model) pairs already used (fan-out)


@dataclass
class Candidate:
    provider: str
    model: str
    score: float
    breakdown: dict[str, float]
    tier: str
    excluded: str | None = None


@dataclass
class RoutingDecision:
    provider: str
    model: str
    effort: str | None
    mode: str
    policy: str
    reasons: list[str]
    candidates: list[dict[str, Any]]
    id: str = field(default_factory=lambda: new_id("rd"))


class RoutingPolicy(Protocol):
    name: str

    def select(self, ctx: RouteContext) -> RoutingDecision: ...

    def record_outcome(self, decision_id: str, outcome: dict[str, Any]) -> None: ...


class HeuristicPolicy:
    name = "heuristic-v1"

    def __init__(self, forge: "Forge"):
        self.forge = forge

    # --- features --------------------------------------------------------------
    def history(self, provider: str, model: str, role: str | None = None) -> dict[str, Any]:
        sql = ("SELECT state, (ended_at - started_at) AS dur FROM runs WHERE kind='agent' AND provider=? AND model=? "
               "AND state IN ('succeeded','failed')")
        params: list[Any] = [provider, model]
        if role:
            sql += " AND role=?"
            params.append(role)
        rows = self.forge.db.query(sql + " ORDER BY started_at DESC LIMIT 200", params)
        n = len(rows)
        ok = sum(1 for r in rows if r["state"] == "succeeded")
        durs = [r["dur"] for r in rows if r["dur"]]
        return {"n": n, "success_rate": (ok + 1) / (n + 2), "avg_duration_s": (sum(durs) / len(durs)) if durs else None}

    def _rate_limited(self, provider: str) -> bool:
        r = self.forge.db.one(
            "SELECT MAX(ended_at) t FROM runs WHERE provider=? AND state='failed' AND error LIKE '%rate%limit%'",
            (provider,))
        return bool(r and r["t"] and time.time() - r["t"] < RATE_LIMIT_COOLDOWN_S)

    def _paid_allowed(self, cfg: ProjectConfig) -> bool:
        return self.forge.settings().paid_api_globally_allowed and cfg.permissions.allow_paid_api

    @staticmethod
    def pick_effort(levels: list[str], tier_needed: int, mode: str, complexity: str) -> str | None:
        if not levels:
            return None
        target = {"low": "low", "medium": "medium", "high": "high"}.get(complexity, "medium")
        idx = EFFORT_ORDER.index(target)
        if mode == "max_quality":
            idx += 1
        elif mode == "economy":
            idx -= 1
        cap = EFFORT_ORDER.index(AUTO_EFFORT_CAP)
        idx = max(1, min(idx, cap))
        usable = [lv for lv in levels if lv in EFFORT_ORDER and EFFORT_ORDER.index(lv) <= cap]
        if not usable:
            return None
        return min(usable, key=lambda lv: abs(EFFORT_ORDER.index(lv) - idx))

    # --- selection ---------------------------------------------------------------
    def select(self, ctx: RouteContext) -> RoutingDecision:
        reg = self.forge.registry
        assert reg is not None
        cfg = self.forge.projects.config(ctx.project_id)
        s = self.forge.settings()
        reasons: list[str] = []

        if ctx.override and ctx.override.get("provider") and ctx.override.get("model"):
            return self._manual(ctx, cfg)
        if ctx.mode == "manual":
            raise RoutingError(f"manual routing requires a provider and model for phase {ctx.phase!r}")

        req = {"low": 0, "medium": 1, "high": 2}.get(ctx.complexity, 1)
        why_req = [f"complexity={ctx.complexity}"]
        if ctx.criticality == "high":
            req += 1
            why_req.append("criticality=high")
        if ctx.mode == "max_quality" and ctx.role in STRONG_ROLES:
            req = 2
            why_req.append(f"max_quality prefers strong models for {ctx.role}")
        if ctx.mode == "economy":
            req -= 1
            why_req.append("economy starts one tier lower")
        if ctx.escalation:
            req += ctx.escalation
            why_req.append(f"escalated {ctx.escalation} tier(s) after failure")
        req = max(0, min(2, req))
        reasons.append(f"Required tier: {TIERS[req]} ({', '.join(why_req)})")

        w = MODE_WEIGHTS[ctx.mode]
        cands: list[Candidate] = []
        for m in reg.models(enabled_only=True):
            p, mid = m["provider"], m["model_id"]
            tier = m["profile"].get("tier", "standard")
            c = Candidate(p, mid, 0.0, {}, tier)
            st = reg.status(p)
            if (p, mid) in ctx.exclude:
                c.excluded = "already used in this fan-out"
            elif not st.get("installed"):
                c.excluded = "provider not installed"
            elif st.get("auth_state") in ("expired", "missing"):
                c.excluded = f"provider auth {st.get('auth_state')}"
            elif m["status"] == "failed":
                c.excluded = f"model failed verification: {m['detail'][:80]}"
            elif m["status"] != "verified" and not s.allow_unverified_models:
                c.excluded = "model not verified (run a probe)"
            elif st.get("billing") == "api" and not self._paid_allowed(cfg):
                c.excluded = "directly billed API not permitted"
            elif self._rate_limited(p):
                c.excluded = "provider rate-limited recently (cooldown)"
            if c.excluded:
                cands.append(c)
                continue
            t = TIERS.index(tier) if tier in TIERS else 1
            b: dict[str, float] = {}
            b["tier_fit"] = (-1.2 * (req - t)) if t < req else (-w["overshoot"] * (t - req))
            # Capability beyond the requirement earns nothing (avoid unnecessary escalation),
            # except in max_quality mode where stronger is always preferred.
            b["quality"] = w["quality"] * (t if ctx.mode == "max_quality" else min(t, req)) / 2
            b["cost"] = -w["cost"] * float(m["profile"].get("cost_index", 1.0)) / 2.5
            h = self.history(p, mid, ctx.role)
            conf = h["n"] / (h["n"] + 5)
            b["history"] = round(conf * (h["success_rate"] - 0.5) * 2, 3)
            if h["avg_duration_s"]:
                b["latency"] = -w["latency"] * min(h["avg_duration_s"] / 600, 1.0)
            b["preference"] = float(m["preference"] or 0)
            if p in cfg.preferred_providers:
                b["project_preference"] = 0.3
            if p in ctx.avoid_providers:
                b["independence"] = -0.8
            prio = m["capabilities"].get("catalog_priority")
            if isinstance(prio, int):
                b["catalog_order"] = -0.01 * prio
            if m["is_mock"]:
                b["mock"] = 0.0
            c.score = round(sum(b.values()), 3)
            c.breakdown = {k: round(v, 3) for k, v in b.items()}
            cands.append(c)

        viable = [c for c in cands if not c.excluded]
        if ctx.independent and ctx.avoid_providers:
            alt = [c for c in viable if c.provider not in ctx.avoid_providers]
            if alt:
                viable = alt
                reasons.append(f"Independent review: excluded {', '.join(ctx.avoid_providers)}")
            else:
                reasons.append("Independent review requested but no other provider is available; "
                               "using same provider (flagged)")
        if not viable:
            raise RoutingError("no available model: " + "; ".join(
                f"{c.provider}/{c.model}: {c.excluded}" for c in cands) if cands else "no models registered")
        viable.sort(key=lambda c: c.score, reverse=True)
        best = viable[0]
        model_row = reg.model(best.provider, best.model)
        levels = model_row["capabilities"].get("reasoning_levels") or reg.get(best.provider).supported_efforts(model_row)
        effort = self.pick_effort(levels, req, ctx.mode, ctx.complexity)
        reasons.append(f"Mode {ctx.mode}: weights quality={w['quality']}, cost={w['cost']}, latency={w['latency']}")
        reasons.append(f"Selected {best.provider}/{best.model} (tier {best.tier}, score {best.score}: "
                       + ", ".join(f"{k} {v:+}" for k, v in best.breakdown.items()) + ")")
        h = self.history(best.provider, best.model, ctx.role)
        reasons.append(f"History for role {ctx.role}: " + (
            f"{h['n']} runs, smoothed success {h['success_rate']:.2f}" if h["n"] else "no recorded runs yet (prior only)"))
        if effort:
            reasons.append(f"Reasoning effort {effort} (complexity {ctx.complexity}, mode {ctx.mode}; "
                           f"auto-selection capped at {AUTO_EFFORT_CAP})")
        if len(viable) > 1:
            r2 = viable[1]
            reasons.append(f"Runner-up: {r2.provider}/{r2.model} (score {r2.score})")
        return RoutingDecision(best.provider, best.model, effort, ctx.mode, self.name, reasons,
                               [self._cand_dict(c) for c in sorted(cands, key=lambda c: (c.excluded is not None, -c.score))])

    def _manual(self, ctx: RouteContext, cfg: ProjectConfig) -> RoutingDecision:
        reg = self.forge.registry
        o = ctx.override or {}
        p, mid = o["provider"], o["model"]
        try:
            m = reg.model(p, mid)
        except KeyError:
            raise RoutingError(f"manual route {p}/{mid}: model not registered")
        st = reg.status(p)
        if not st.get("installed"):
            raise RoutingError(f"manual route {p}/{mid}: provider not installed")
        if st.get("billing") == "api" and not self._paid_allowed(cfg):
            raise RoutingError(f"manual route {p}/{mid}: directly billed API not permitted for this project")
        if m["status"] == "failed":
            raise RoutingError(f"manual route {p}/{mid}: model failed verification ({m['detail'][:120]})")
        levels = m["capabilities"].get("reasoning_levels") or reg.get(p).supported_efforts(m)
        effort = o.get("effort")
        if effort and levels and effort not in levels:
            raise RoutingError(f"effort {effort!r} not supported by {p}/{mid}; supported: {levels}")
        reasons = [f"Manual selection for phase {ctx.phase}: {p}/{mid}" + (f" effort={effort}" if effort else "")]
        if m["status"] != "verified":
            reasons.append("Warning: model not yet verified; the run itself will verify it")
        return RoutingDecision(p, mid, effort, "manual", self.name, reasons, [])

    @staticmethod
    def _cand_dict(c: Candidate) -> dict[str, Any]:
        return {"provider": c.provider, "model": c.model, "tier": c.tier, "score": c.score,
                "breakdown": c.breakdown, "excluded": c.excluded}

    def record_outcome(self, decision_id: str, outcome: dict[str, Any]) -> None:
        # The heuristic learns nothing directly; outcomes feed `history()` via the runs table.
        pass


class Router:
    """Facade that persists decisions and delegates to the active policy."""

    def __init__(self, forge: "Forge", policy: RoutingPolicy | None = None):
        self.forge = forge
        self.policy: RoutingPolicy = policy or HeuristicPolicy(forge)

    def route(self, ctx: RouteContext, *, persist: bool = True) -> RoutingDecision:
        d = self.policy.select(ctx)
        if persist:
            self.forge.db.execute(
                "INSERT INTO routing_decisions(id,task_id,phase,mode,policy,provider,model,effort,reasons,candidates,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d.id, ctx.task_id, ctx.phase, d.mode, d.policy, d.provider, d.model, d.effort, dumps(d.reasons),
                 dumps(d.candidates), now()))
            self.forge.bus.emit("routing.decision", f"{ctx.phase}: {d.provider}/{d.model}"
                                + (f" ({d.effort})" if d.effort else "") + f" — {d.reasons[-1] if d.reasons else ''}",
                                project_id=ctx.project_id, task_id=ctx.task_id,
                                data={"decision_id": d.id, "provider": d.provider, "model": d.model, "effort": d.effort})
        return d

    def attach_run(self, decision_id: str, run_id: str) -> None:
        self.forge.db.execute("UPDATE routing_decisions SET run_id=? WHERE id=?", (run_id, decision_id))

    def record_outcome(self, decision_id: str, outcome: dict[str, Any]) -> None:
        self.forge.db.execute("UPDATE routing_decisions SET outcome=? WHERE id=?", (dumps(outcome), decision_id))
        self.policy.record_outcome(decision_id, outcome)

    def decisions(self, task_id: str) -> list[dict]:
        rows = self.forge.db.query("SELECT * FROM routing_decisions WHERE task_id=? ORDER BY created_at", (task_id,))
        for r in rows:
            r["reasons"] = loads(r["reasons"], [])
            r["candidates"] = loads(r["candidates"], [])
            r["outcome"] = loads(r["outcome"])
        return rows

    def model_stats(self) -> list[dict]:
        rows = self.forge.db.query(
            "SELECT provider, model, role, COUNT(*) n, SUM(state='succeeded') ok, AVG(ended_at-started_at) avg_s "
            "FROM runs WHERE kind='agent' AND ended_at IS NOT NULL GROUP BY provider, model, role ORDER BY provider, model")
        return rows
