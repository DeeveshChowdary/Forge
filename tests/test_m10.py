"""M10 regression tests: the t_fa827e885e2d failure, collaboration policies, literature workflow,
per-phase limits, checkpoints, continuation, recovery."""

import json
import subprocess
from pathlib import Path

import pytest

from forge.collaboration import PolicyError
from forge.db import dumps, now
from forge.engine import classify_failure, submit_task
from forge.providers.mock import MockAdapter
from tests.conftest import make_repo
from tests.test_engine import MUL_IMPL, TEST_CMD, runs

FAILED_OBJECTIVE = """# R4 Milestone 1 — Deep Prior-Art Analysis and Novelty Validation

Research directory: R4-computation-migration/

## Phase 0 — Verify the environment

1. Verify that Forge registered the repository. Confirm Claude and Codex are available and authenticated.
2. Verify Zotero access and the existing bibliography. Identify missing dependencies and environment issues.

## Phase 1 — Read existing research

Read R4-computation-migration/AGENTS.md, R4-computation-migration/docs/CURRENT_STATE.md,
R4-computation-migration/docs/HANDOFF.md, R4-computation-migration/literature/reviews/initial_review.md,
R4-computation-migration/literature/matrices/novelty_matrix.md, R4-computation-migration/proposals/hypotheses.md.

## Phase 2 — Independent model investigations

Claude: analyze theoretical foundations of the literature. Codex: analyze prior-art systems papers.

## Phase 3 — Novelty assessment

Compare against the closest prior art; literature and citation coverage must be checked for each paper.

## Phase 4 — Decision

Produce a GO/PIVOT/REJECT/INCONCLUSIVE decision with citations.
""" + ("Additional literature context. " * 80)


class NamedMock(MockAdapter):
    """The deterministic mock, registered under a real provider name to test multi-provider policies."""
    is_mock = True

    def __init__(self, name):
        self.name = name
        self.display_name = f"{name} (mock)"


def two_providers(app, *, codex=True):
    reg = app.registry
    for prov in ("claude_code", "codex") if codex else ("claude_code",):
        reg.adapters[prov] = NamedMock(prov)
        app.db.execute("INSERT OR REPLACE INTO provider_status(provider,data,checked_at) VALUES (?,?,?)",
                       (prov, dumps({"installed": True, "auth_state": "ok", "billing": "subscription"}), now()))
        app.db.execute(
            "INSERT OR REPLACE INTO models(provider,model_id,display_name,source,status,capabilities,profile,enabled,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (prov, f"{prov}-std", f"{prov}-std", "configured", "verified",
             dumps({"reasoning_levels": ["low", "medium", "high"]}), dumps({"tier": "standard", "cost_index": 1.0}),
             1, now()))
    reg.adapters.pop("mock", None)


@pytest.fixture
def research_repo(tmp_path):
    return make_repo(tmp_path / "research", {"README.md": "# research\nseed\n",
                                             "R4/docs/STATE.md": "state\n", "tests/test_ok.py": "def test_ok():\n    pass\n"})


def agent_providers(app, tid, phase):
    return [r["provider"] for r in app.db.query(
        "SELECT provider FROM runs WHERE task_id=? AND phase=? AND kind='agent' ORDER BY started_at", (tid, phase))]


# ---------------------------------------------------------------- reproduce t_fa827e885e2d
async def test_reproduce_turn_limit_failure_no_identical_retry(mock_app, py_repo):
    """Old behaviour: experiment hit 40 steps twice with an identical from-scratch retry.
    New: one continuation from the checkpoint (same agent, resumed session, files kept), then a clear stop."""
    app = mock_app
    p = app.projects.import_folder(str(py_repo), config={"test_command": TEST_CMD})
    t = submit_task(app, p["id"], title="R4 M1", objective="Survey prior art", workflow="research",
                    metadata={"mock": {"limit_always": {"experimenter": True},
                                       "partial_files": {"deep_review.md": "# partial review\n"}}})
    await app.engine.run_until_idle([t["id"]], timeout=60)
    t = app.tasks.get(t["id"])
    assert t["state"] == "FAILED"
    exp = [r for r in runs(app, t["id"]) if r["phase"] == "experiment"]
    assert len(exp) == 2, "exactly one continuation, never an unbounded or identical retry loop"
    actions = [f["action"] for f in t["metadata"]["failures"]]
    assert actions == ["continuation", "stop"]
    assert all(f["classification"] == "limit_exhausted" for f in t["metadata"]["failures"])
    assert "not retried automatically" in app.db.one(
        "SELECT message FROM events WHERE task_id=? AND type='task.state' ORDER BY id DESC LIMIT 1", (t["id"],))["message"]
    assert "raise the phase limit" in t["error"]
    second = app.db.one("SELECT output_text, prompt FROM runs WHERE task_id=? AND phase='experiment' ORDER BY "
                        "started_at DESC LIMIT 1", (t["id"],))
    assert "CONTINUATION" in second["prompt"] and "deep_review.md" in second["prompt"]
    fa = app.engine.failure_analysis(t)
    assert fa["last_failure"]["classification"] == "limit_exhausted"
    assert fa["completed_phases"] == ["research", "hypothesis"]
    assert fa["recoverable"] and "deep_review.md" in fa["recoverable"][0]["files"]
    assert fa["checkpoints"] and fa["checkpoints"][0]["ref"].startswith("refs/forge/checkpoints/")
    assert gitops_clean(t["worktree_path"]), "worktree is clean after failure"
    with pytest.raises(ValueError):
        app.engine.retry_task(t["id"])              # identical retry refused
    t = app.engine.retry_task(t["id"], phase_max_turns=150)
    assert t["state"] == "READY" and t["budget"]["phase_limits"]["experiment"]["max_turns"] == 150


def gitops_clean(path):
    return subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout == ""


async def test_continuation_resumes_with_preserved_work(mock_app, py_repo):
    app = mock_app
    p = app.projects.import_folder(str(py_repo), config={"test_command": TEST_CMD})
    t = submit_task(app, p["id"], title="x", objective="y", workflow="research",
                    metadata={"mock": {"limit": {"experimenter": 1}, "partial_files": {"partial.md": "half\n"},
                                       "files": {"result.md": "done\n"}}})
    await app.engine.run_until_idle([t["id"]], timeout=60)
    t = app.tasks.get(t["id"])
    out = app.db.one("SELECT output_text FROM runs WHERE task_id=? AND phase='experiment' AND state='succeeded'",
                     (t["id"],))["output_text"]
    assert "resumed=mock-session-experimenter" in out, "same session resumed"
    assert "partial.md" in out.split("seen=")[1], "continuation saw the preserved partial file"
    files = subprocess.run(["git", "ls-files"], cwd=t["worktree_path"], capture_output=True, text=True).stdout
    assert "partial.md" in files and "result.md" in files
    assert t["state"] == "COMPLETED" or t["state"] == "AWAITING_APPROVAL"


async def test_limit_without_progress_stops_immediately(mock_app, py_repo):
    app = mock_app
    p = app.projects.import_folder(str(py_repo), config={"test_command": TEST_CMD})
    t = submit_task(app, p["id"], title="x", objective="y", workflow="research",
                    metadata={"mock": {"limit_always": {"experimenter": True}, "no_session": True}})
    await app.engine.run_until_idle([t["id"]], timeout=60)
    t = app.tasks.get(t["id"])
    assert t["state"] == "FAILED"
    assert len([r for r in runs(app, t["id"]) if r["phase"] == "experiment"]) == 1
    assert t["metadata"]["failures"][0]["action"] == "stop"


async def test_per_phase_limits_applied(mock_app, py_repo):
    app = mock_app
    p = app.projects.import_folder(str(py_repo), config={"test_command": TEST_CMD})
    t = submit_task(app, p["id"], title="x", objective="y", workflow="plan_implement_test_review",
                    budget={"phase_limits": {"implement": {"max_turns": 7, "max_runtime_s": 99}}},
                    metadata={"mock": {"files": MUL_IMPL}})
    await app.engine.run_until_idle([t["id"]], timeout=60)
    us = {r["phase"]: json.loads(r["usage"]) for r in app.db.query(
        "SELECT phase, usage FROM runs WHERE task_id=? AND kind='agent'", (t["id"],))}
    assert us["implement"]["max_turns"] == 7 and us["implement"]["max_runtime_s"] == 99
    assert us["plan"]["max_turns"] == 40


def test_retry_classification():
    assert classify_failure("turn_limit") == classify_failure("timeout") == "limit_exhausted"
    assert classify_failure("auth") == "needs_user"
    assert classify_failure("other") == "transient"


# ---------------------------------------------------------------- preflight
def test_preflight_flags_the_failed_task(mock_app, research_repo):
    app = mock_app
    p = app.projects.import_folder(str(research_repo))
    from forge import preflight, workflows
    r = preflight.run(app, p["id"], workflows.get("research"), FAILED_OBJECTIVE, policy="auto", mode="balanced",
                      budget={"max_turns": 40})
    w = " | ".join(r["warnings"])
    assert "literature_review" in w
    assert "both Claude and Codex" in w
    assert "exceeds its limit" in w
    assert "decompos" in w and r["recommend_decompose"]
    assert "environment" in w
    assert r["route_plan"]["phases"][0]["agents"][0]["provider"] == "mock"


# ---------------------------------------------------------------- collaboration policies
async def test_independent_research_literature_workflow(mock_app, research_repo):
    app = mock_app
    two_providers(app)
    p = app.projects.import_folder(str(research_repo))
    t = submit_task(app, p["id"], title="Prior art", objective="Assess novelty of R4", workflow="literature_review",
                    approval_policy="never_merge")
    assert t["metadata"]["collaboration"] == "independent_research"
    plan = t["metadata"]["route_plan"]
    assert [a["provider"] for a in plan["phases"][1]["agents"]] == ["claude_code", "codex"]
    await app.engine.run_until_idle([t["id"]], timeout=90)
    t = app.tasks.get(t["id"])
    assert t["state"] == "COMPLETED", t["error"]
    assert sorted(agent_providers(app, t["id"], "independent_analysis")) == ["claude_code", "codex"]
    assert sorted(agent_providers(app, t["id"], "cross_review")) == ["claude_code", "codex"]
    # independent reports preserved separately; cross-reviewers saw the OTHER model's analysis
    reports = sorted((Path(t["worktree_path"]) / "forge-reports" / t["id"]).glob("*.md"))
    names = [r.name for r in reports]
    assert len(names) == 7 and any("independent_analysis-claude" in n for n in names) and \
        any("independent_analysis-codex" in n for n in names) and any("decision" in n for n in names)
    for prov in ("claude_code", "codex"):
        prompt = app.db.one("SELECT prompt FROM runs WHERE task_id=? AND phase='cross_review' AND provider=?",
                            (t["id"], prov))["prompt"]
        other = "codex" if prov == "claude_code" else "claude_code"
        i = prompt.index("OTHER MODEL'S ANALYSIS")
        assert other in prompt[i:i + 200]
    # citation integrity: unresolvable source flagged in the report, not dropped
    lit = next(r for r in reports if "literature_review" in r.name).read_text()
    assert "NOT FOUND: `missing/nowhere.md`" in lit
    dec = app.artifacts.latest(t["id"], "decision")["data"]
    assert dec["decision"] in ("GO", "PIVOT", "REJECT", "INCONCLUSIVE")
    # no executable experiment, no test phase
    assert not [r for r in runs(app, t["id"]) if r["kind"] == "test"]
    log = subprocess.run(["git", "log", "--format=%s", f"{t['base_commit']}..{t['branch']}"], cwd=research_repo,
                         capture_output=True, text=True).stdout
    assert log.count("report by") == 7, "each report committed as a durable checkpoint"


def test_independent_research_requires_both_providers(mock_app, research_repo):
    app = mock_app
    two_providers(app, codex=False)
    p = app.projects.import_folder(str(research_repo))
    with pytest.raises(PolicyError) as e:
        submit_task(app, p["id"], title="x", objective="y", workflow="literature_review")
    assert "codex" in str(e.value)


async def test_policy_blocks_instead_of_fallback_at_runtime(mock_app, research_repo):
    app = mock_app
    two_providers(app)
    p = app.projects.import_folder(str(research_repo))
    t = submit_task(app, p["id"], title="x", objective="y", workflow="literature_review")
    app.db.execute("UPDATE provider_status SET data=? WHERE provider='codex'",
                   (dumps({"installed": True, "auth_state": "expired", "billing": "subscription"}),))
    await app.engine.run_until_idle([t["id"]], timeout=60)
    t = app.tasks.get(t["id"])
    assert t["state"] == "BLOCKED" and "independent_research" in (t["error"] or "") + t["phase"] or \
        "collaboration" in (t["error"] or "")
    assert agent_providers(app, t["id"], "independent_analysis") == [], "no silent single-provider fallback"


async def test_implement_review_and_cross_model(mock_app, py_repo):
    app = mock_app
    two_providers(app)
    p = app.projects.import_folder(str(py_repo), config={"test_command": TEST_CMD})
    for policy in ("implement_review", "cross_model"):
        t = submit_task(app, p["id"], title=policy, objective="Add mul", workflow="plan_implement_test_review",
                        approval_policy="never_merge",
                        metadata={"collaboration": policy, "mock": {"files": MUL_IMPL}})
        await app.engine.run_until_idle([t["id"]], timeout=60)
        impl = agent_providers(app, t["id"], "implement")
        rev = agent_providers(app, t["id"], "review")
        assert impl and rev and set(impl).isdisjoint(rev), (policy, impl, rev)
        used = {r["provider"] for r in runs(app, t["id"]) if r["provider"]}
        assert len(used) >= 2


def test_cross_model_with_one_provider_is_a_blocker(mock_app, py_repo):
    app = mock_app
    p = app.projects.import_folder(str(py_repo))
    with pytest.raises(PolicyError):
        submit_task(app, p["id"], title="x", objective="y", workflow="plan_implement_test_review",
                    metadata={"collaboration": "cross_model"})


def test_manual_policy_requires_every_phase(mock_app, py_repo):
    app = mock_app
    p = app.projects.import_folder(str(py_repo))
    with pytest.raises(PolicyError):
        submit_task(app, p["id"], title="x", objective="y", workflow="plan_implement_test_review",
                    metadata={"collaboration": "manual"},
                    routing_overrides={"plan": {"provider": "mock", "model": "mock-fast"}})
    route = {"provider": "mock", "model": "mock-fast"}
    t = submit_task(app, p["id"], title="x", objective="y", workflow="plan_implement_test_review",
                    metadata={"collaboration": "manual"},
                    routing_overrides={"plan": route, "implement": route, "review": route})
    assert [a["agents"][0]["model"] for a in t["metadata"]["route_plan"]["phases"] if a["agents"]] == ["mock-fast"] * 3


# ---------------------------------------------------------------- decomposition & recovery
def test_section_decomposition(mock_app, research_repo):
    app = mock_app
    p = app.projects.import_folder(str(research_repo))
    t = submit_task(app, p["id"], title="M1", objective=FAILED_OBJECTIVE, workflow="analyze", execution_mode="worktree",
                    metadata={"decompose": "sections"})
    kids = app.tasks.list(parent_id=t["id"])
    assert len(kids) == 5 and t["state"] == "BLOCKED"
    by_title = {k["title"].split("— ", 1)[1]: k for k in kids}
    assert by_title["Phase 1 — Read existing research"]["depends_on"] == [by_title["Phase 0 — Verify the environment"]["id"]]


async def test_recover_checkpoint_to_branch_needs_approval(mock_app, py_repo):
    app = mock_app
    p = app.projects.import_folder(str(py_repo), config={"test_command": TEST_CMD})
    t = submit_task(app, p["id"], title="x", objective="y", workflow="research",
                    metadata={"mock": {"limit_always": {"experimenter": True},
                                       "partial_files": {"draft.md": "valuable draft\n"}}})
    await app.engine.run_until_idle([t["id"]], timeout=60)
    t = app.tasks.get(t["id"])
    diff = app.engine.failure_analysis(t)["recoverable"][-1]
    before = subprocess.run(["git", "branch"], cwd=py_repo, capture_output=True, text=True).stdout
    ap = app.engine.request_recovery(t["id"], diff["artifact_id"])
    assert ap["kind"] == "recovery" and "recovery" not in before
    app.engine.apply_approval(ap["id"], True)
    branches = subprocess.run(["git", "branch", "--list", "forge/recovery-*"], cwd=py_repo, capture_output=True,
                              text=True).stdout.split()
    assert branches
    content = subprocess.run(["git", "show", f"{branches[-1]}:draft.md"], cwd=py_repo, capture_output=True,
                             text=True).stdout
    assert content == "valuable draft\n"
    assert gitops_clean(py_repo), "user's working tree untouched"


def test_legacy_failed_task_analysis(mock_app, py_repo):
    """Tasks that failed before M10 (no failure metadata) still show recoverable work and retry rules."""
    app = mock_app
    p = app.projects.import_folder(str(py_repo))
    t = app.tasks.create(p["id"], title="legacy", objective="o", workflow="research")
    app.db.execute("INSERT INTO runs(id,task_id,project_id,phase,role,kind,provider,model,state,usage,error,started_at,"
                   "ended_at) VALUES ('r_l1',?,?,'research','researcher','agent','mock','m','succeeded','{}',NULL,1,2)",
                   (t["id"], p["id"]))
    app.db.execute("INSERT INTO runs(id,task_id,project_id,phase,role,kind,provider,model,state,usage,error,started_at,"
                   "ended_at) VALUES ('r_l2',?,?,'experiment','experimenter','agent','mock','m','failed',?, "
                   "'turn limit 40 exceeded',3,9)", (t["id"], p["id"], dumps({"turns": 41})))
    app.artifacts.add(p["id"], t["id"], "diff", "Partial changes discarded (failed experiment) in phase experiment",
                      "diff --git a/x.md b/x.md\n--- /dev/null\n+++ b/x.md\n+line\n")
    fa = app.engine.failure_analysis(app.tasks.get(t["id"]))
    assert fa["last_failure"]["classification"] == "limit_exhausted" and fa["last_failure"]["steps_used"] == 41
    assert fa["recoverable"][0]["files"] == ["x.md"] and fa["completed_phases"] == ["research"]
