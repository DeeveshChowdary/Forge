"""Router selection tests using stub providers with controlled status/profiles."""

import pytest

from forge.db import dumps, now
from forge.providers.mock import MockAdapter
from forge.router import HeuristicPolicy, RouteContext, RoutingError
from tests.conftest import make_repo


class Stub(MockAdapter):
    is_mock = False

    def __init__(self, name):
        self.name = name
        self.display_name = name


def setup_providers(app, spec):
    """spec: {provider: {"billing":..., "auth":..., "models": {id: (tier, status, levels)}}}"""
    reg = app.registry
    for prov, ps in spec.items():
        reg.adapters[prov] = Stub(prov)
        app.db.execute("INSERT OR REPLACE INTO provider_status(provider,data,checked_at) VALUES (?,?,?)",
                       (prov, dumps({"installed": True, "auth_state": ps.get("auth", "ok"),
                                     "billing": ps.get("billing", "subscription")}), now()))
        for mid, (tier, status, levels) in ps["models"].items():
            app.db.execute(
                "INSERT OR REPLACE INTO models(provider,model_id,display_name,source,status,capabilities,profile,enabled,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (prov, mid, mid, "configured", status, dumps({"reasoning_levels": levels}),
                 dumps({"tier": tier, "cost_index": {"fast": 0.4, "standard": 1.0, "strong": 2.5}[tier]}), 1, now()))


LEVELS = ["low", "medium", "high", "xhigh", "max", "ultra"]


@pytest.fixture
def routed(app, tmp_path):
    app.registry.adapters.clear()
    setup_providers(app, {
        "alpha": {"models": {"a-fast": ("fast", "verified", LEVELS), "a-std": ("standard", "verified", LEVELS),
                             "a-strong": ("strong", "verified", LEVELS)}},
        "beta": {"models": {"b-std": ("standard", "verified", ["low", "medium", "high"]),
                            "b-strong": ("strong", "verified", ["low", "medium", "high"])}},
    })
    p = app.projects.import_folder(str(make_repo(tmp_path / "rp")))
    return app, p


def ctx(p, **kw):
    base = dict(project_id=p["id"], task_id="t_x", phase="implement", role="implementer")
    base.update(kw)
    return RouteContext(**base)


def pick(app, c):
    return HeuristicPolicy(app).select(c)


def test_balanced_matches_complexity(routed):
    app, p = routed
    assert pick(app, ctx(p, complexity="medium")).model in ("a-std", "b-std")
    assert pick(app, ctx(p, complexity="high")).model in ("a-strong", "b-strong")
    assert pick(app, ctx(p, complexity="low")).model == "a-fast"


def test_economy_prefers_cheaper_and_escalates(routed):
    app, p = routed
    assert pick(app, ctx(p, mode="economy", complexity="medium")).model == "a-fast"
    d = pick(app, ctx(p, mode="economy", complexity="medium", escalation=1))
    assert d.model in ("a-std", "b-std")
    assert any("escalated" in r for r in d.reasons)


def test_max_quality_prefers_strong(routed):
    app, p = routed
    d = pick(app, ctx(p, mode="max_quality", role="reviewer", phase="review", complexity="low"))
    assert d.model in ("a-strong", "b-strong")


def test_effort_selection_is_capped_and_supported(routed):
    app, p = routed
    d = pick(app, ctx(p, mode="max_quality", complexity="high"))
    assert d.effort in ("xhigh", "high")
    assert d.effort not in ("max", "ultra")
    d = pick(app, ctx(p, complexity="low"))
    assert d.effort == "low"


def test_independent_review_avoids_implementer_provider(routed):
    app, p = routed
    d = pick(app, ctx(p, role="reviewer", phase="review", avoid_providers=["alpha"], independent=True))
    assert d.provider == "beta"
    d2 = pick(app, ctx(p, role="reviewer", phase="review", avoid_providers=["beta"], independent=True))
    assert d2.provider == "alpha"


def test_unavailable_models_are_excluded(app, tmp_path):
    app.registry.adapters.clear()
    setup_providers(app, {
        "alpha": {"auth": "expired", "models": {"a-std": ("standard", "verified", LEVELS)}},
        "beta": {"models": {"b-std": ("standard", "unverified", LEVELS), "b-bad": ("standard", "failed", LEVELS)}},
        "gamma": {"billing": "api", "models": {"g-std": ("standard", "verified", LEVELS)}},
    })
    p = app.projects.import_folder(str(make_repo(tmp_path / "x")))
    with pytest.raises(RoutingError) as e:
        pick(app, ctx(p))
    msg = str(e.value)
    assert "auth expired" in msg and "not verified" in msg and "failed verification" in msg
    assert "directly billed API not permitted" in msg
    app.save_settings({"allow_unverified_models": True})
    assert pick(app, ctx(p)).model == "b-std"


def test_paid_api_requires_both_switches(app, tmp_path):
    app.registry.adapters.clear()
    setup_providers(app, {"gamma": {"billing": "api", "models": {"g-std": ("standard", "verified", LEVELS)}}})
    p = app.projects.import_folder(str(make_repo(tmp_path / "y")), config={"permissions": {"allow_paid_api": True}})
    with pytest.raises(RoutingError):
        pick(app, ctx(p))
    app.save_settings({"paid_api_globally_allowed": True})
    assert pick(app, ctx(p)).provider == "gamma"


def test_manual_mode(routed):
    app, p = routed
    with pytest.raises(RoutingError):
        pick(app, ctx(p, mode="manual"))
    d = pick(app, ctx(p, mode="manual", override={"provider": "beta", "model": "b-std", "effort": "high"}))
    assert (d.provider, d.model, d.effort, d.mode) == ("beta", "b-std", "high", "manual")
    with pytest.raises(RoutingError):
        pick(app, ctx(p, override={"provider": "beta", "model": "b-std", "effort": "ultra"}))
    with pytest.raises(RoutingError):
        pick(app, ctx(p, override={"provider": "beta", "model": "nope"}))


def test_history_influences_score_with_sample_size(routed):
    app, p = routed
    t = app.tasks.create(p["id"], title="h", objective="h", workflow="analyze", execution_mode="read_only")
    for i in range(10):
        app.db.execute("INSERT INTO runs(id,task_id,project_id,phase,role,kind,provider,model,state,started_at,ended_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (f"r{i}", t["id"], p["id"], "implement", "implementer", "agent", "alpha", "a-std", "failed",
                        now() - 10, now()))
    d = pick(app, ctx(p, complexity="medium"))
    assert d.model == "b-std"
    assert any("History" in r for r in d.reasons)


def test_decisions_are_persisted_with_reasons(routed):
    app, p = routed
    d = app.router.route(ctx(p))
    rows = app.router.decisions("t_x")
    assert rows[0]["id"] == d.id and rows[0]["reasons"] and rows[0]["candidates"]
