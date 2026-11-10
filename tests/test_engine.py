"""Mock-agent integration tests for the orchestration engine.

These exercise the real engine, real git worktrees and real test commands; only
the model is replaced by the deterministic mock provider.
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from forge.engine import Engine, submit_task
from tests.conftest import CALC_FILES, make_repo

MUL_IMPL = {
    "calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_mul.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n",
}
MUL_BROKEN = {
    "calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n",
    "tests/test_mul.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n",
}
TEST_CMD = "{python} -m pytest -q -p no:cacheprovider"


def project(app, repo, **cfg):
    base = {"test_command": TEST_CMD}
    base.update(cfg)
    return app.projects.import_folder(str(repo), config=base)


def runs(app, tid):
    return app.db.query("SELECT phase, role, kind, state, provider, model FROM runs WHERE task_id=? ORDER BY started_at", (tid,))


async def drive(app, tids, timeout=60):
    await app.engine.run_until_idle(tids, timeout=timeout)


async def test_happy_path_with_merge_approval(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="Add mul", objective="Add mul(a, b) with a test",
                    metadata={"mock": {"files": MUL_IMPL}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "AWAITING_APPROVAL"
    assert [r["phase"] for r in runs(app, t["id"])] == ["plan", "implement", "test", "review"]
    assert all(r["state"] == "succeeded" for r in runs(app, t["id"]))
    # The user's tree is untouched until approval.
    assert "mul" not in (py_repo / "calc.py").read_text()
    approval = app.approvals.list(task_id=t["id"], status="pending")[0]
    assert approval["kind"] == "merge"
    app.engine.apply_approval(approval["id"], True)
    t = app.tasks.get(t["id"])
    assert t["state"] == "COMPLETED" and "merged" in t["disposition"]
    assert "def mul" in (py_repo / "calc.py").read_text()
    kinds = {a["kind"] for a in app.artifacts.list(task_id=t["id"])}
    assert {"plan", "handoff", "test_report", "review", "diff"} <= kinds
    assert app.memory.list(p["id"], kind="user_decision")


async def test_review_requests_changes_then_fix_loop(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="mul", objective="Add mul",
                    metadata={"mock": {"files": MUL_IMPL, "fix_files": {"NOTES.md": "edge cases handled\n"},
                                       "reviews": ["request_changes", "approve"]}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "AWAITING_APPROVAL"
    assert t["review_cycle"] == 1
    assert [r["phase"] for r in runs(app, t["id"])] == ["plan", "implement", "test", "review", "fix", "test", "review"]


async def test_review_loop_is_bounded(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_review_cycles": 1})
    t = submit_task(app, p["id"], title="mul", objective="Add mul",
                    metadata={"mock": {"files": MUL_IMPL, "fix_files": {"x.txt": "1"},
                                       "reviews": ["request_changes"]}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "AWAITING_APPROVAL"
    pending = app.approvals.list(task_id=t["id"], status="pending")
    assert [a["kind"] for a in pending] == ["review_unresolved"]
    assert sum(1 for r in runs(app, t["id"]) if r["phase"] == "review") == 2  # initial + 1 cycle, then stop
    # Rejecting fails the task.
    app.engine.apply_approval(pending[0]["id"], False)
    assert app.tasks.get(t["id"])["state"] == "FAILED"


async def test_failing_tests_trigger_fix_then_pass(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="mul", objective="Add mul",
                    metadata={"mock": {"files": MUL_BROKEN, "fix_files": MUL_IMPL}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    tests = [a for a in app.artifacts.list(task_id=t["id"], kind="test_report")]
    assert [a["data"]["passed"] for a in tests] == [False, True]
    assert t["state"] == "AWAITING_APPROVAL"


async def test_tests_failing_after_cycles_fails_task(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_review_cycles": 1})
    t = submit_task(app, p["id"], title="mul", objective="Add mul",
                    metadata={"mock": {"files": MUL_BROKEN, "fix_files": {"README.md": "still broken"}}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "FAILED"
    assert "tests failing" in t["disposition"]


async def test_retry_then_success(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_retries": 1})
    t = submit_task(app, p["id"], title="mul", objective="Add mul",
                    metadata={"mock": {"files": MUL_IMPL, "fail": {"implementer": 1}}})
    await drive(app, [t["id"]])
    r = [x for x in runs(app, t["id"]) if x["phase"] == "implement"]
    assert [x["state"] for x in r] == ["failed", "succeeded"]
    assert app.tasks.get(t["id"])["state"] == "AWAITING_APPROVAL"


async def test_retries_exhausted(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_retries": 1})
    t = submit_task(app, p["id"], title="mul", objective="Add mul",
                    metadata={"mock": {"files": MUL_IMPL, "fail": {"implementer": 5}}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "FAILED"
    assert len([x for x in runs(app, t["id"]) if x["phase"] == "implement"]) == 2
    assert app.memory.list(p["id"], kind="failure")


async def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_cancel_running_task(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="slow", objective="slow",
                    metadata={"mock": {"files": MUL_IMPL, "sleep_s": 30}})
    runner = asyncio.create_task(drive(app, [t["id"]], timeout=30))
    await _wait_for(lambda: app.tasks.get(t["id"])["state"] == "PLANNING")
    app.tasks.request_cancel(t["id"])  # as the CLI/UI would: sets a flag in the DB
    await runner
    t = app.tasks.get(t["id"])
    assert t["state"] == "CANCELLED"
    assert runs(app, t["id"])[-1]["state"] == "cancelled"


async def test_pause_and_resume(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="pausable", objective="x",
                    metadata={"mock": {"files": MUL_IMPL, "sleep_s": 0.6}})
    runner = asyncio.create_task(drive(app, [t["id"]], timeout=30))
    await _wait_for(lambda: app.tasks.get(t["id"])["phase"] == "implement")
    app.tasks.request_pause(t["id"])
    await runner
    t = app.tasks.get(t["id"])
    assert t["state"] == "PAUSED" and t["phase"] == "implement"
    app.tasks.resume(t["id"])
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "AWAITING_APPROVAL"
    plan_runs = [r for r in runs(app, t["id"]) if r["phase"] == "plan"]
    assert len(plan_runs) == 1, "completed phases must not re-run after resume"


async def test_crash_recovery_no_duplicate_execution(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="crashy", objective="x", metadata={"mock": {"files": MUL_IMPL}})
    # Simulate a crashed engine: plan already completed; implement was mid-flight with an
    # orphaned agent process and partial uncommitted edits in the worktree.
    eng_dead = Engine(app)
    eng_dead._ensure_workspace(app.tasks.get(t["id"]))
    task = app.tasks.get(t["id"])
    app.tasks.transition(t["id"], "PLANNING", "sim", phase="plan")
    app.db.execute("INSERT INTO runs(id,task_id,project_id,phase,role,kind,provider,model,state,engine_id,started_at,ended_at)"
                   " VALUES ('r_done',?,?,'plan','planner','agent','mock','mock-standard','succeeded','eng_dead',?,?)",
                   (t["id"], p["id"], time.time(), time.time()))
    app.artifacts.add(p["id"], t["id"], "plan", "plan (from before crash)", {"summary": "s", "steps": []}, run_id="r_done")
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    app.tasks.transition(t["id"], "EXECUTING", "sim", phase="implement", lease_owner="eng_dead",
                         lease_expires=time.time() - 1)
    app.db.execute("INSERT INTO runs(id,task_id,project_id,phase,role,kind,provider,model,state,engine_id,pid,started_at)"
                   " VALUES ('r_mid',?,?,'implement','implementer','agent','mock','mock-standard','running','eng_dead',?,?)",
                   (t["id"], p["id"], orphan.pid, time.time()))
    Path(task["worktree_path"], "partial.txt").write_text("half-written")

    summary = app.engine.recover()
    assert summary["runs_interrupted"] == 1 and summary["processes_killed"] == 1 and summary["tasks_requeued"] == 1
    orphan.wait(timeout=5)
    assert not Path(task["worktree_path"], "partial.txt").exists(), "partial edits are saved as an artifact then reset"
    assert any("Partial changes" in a["title"] for a in app.artifacts.list(task_id=t["id"], kind="diff"))
    await drive(app, [t["id"]])
    r = runs(app, t["id"])
    assert [x["phase"] for x in r if x["phase"] == "plan"] == ["plan"], "plan must not run twice"
    assert {x["state"] for x in r if x["phase"] == "implement"} == {"interrupted", "succeeded"}
    assert app.tasks.get(t["id"])["state"] == "AWAITING_APPROVAL"


async def test_worktree_isolation_and_dirty_tree_protection(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    (py_repo / "calc.py").write_text("# user's uncommitted work\n" + CALC_FILES["calc.py"])
    t1 = submit_task(app, p["id"], title="a", objective="a", metadata={"mock": {"files": MUL_IMPL}})
    t2 = submit_task(app, p["id"], title="b", objective="b", metadata={"mock": {"files": {"b.txt": "b"}}})
    await drive(app, [t1["id"], t2["id"]])
    a, b = app.tasks.get(t1["id"]), app.tasks.get(t2["id"])
    assert a["worktree_path"] != b["worktree_path"] and a["branch"] != b["branch"]
    assert Path(a["worktree_path"]).is_dir() and Path(b["worktree_path"]).is_dir()
    assert (py_repo / "calc.py").read_text().startswith("# user's uncommitted work")
    # Merge is refused while the user's tree is dirty; the user's change is preserved.
    ap = app.approvals.list(task_id=a["id"], status="pending")[0]
    app.engine.apply_approval(ap["id"], True)
    assert app.tasks.get(a["id"])["state"] == "BLOCKED"
    assert (py_repo / "calc.py").read_text().startswith("# user's uncommitted work")


async def test_runtime_limit(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_runtime_s": 1, "max_retries": 0})
    t = submit_task(app, p["id"], title="slow", objective="x",
                    metadata={"mock": {"files": MUL_IMPL, "sleep_s": 45}})
    await drive(app, [t["id"]], timeout=30)
    t = app.tasks.get(t["id"])
    assert t["state"] == "FAILED" and "timed out" in t["error"]


async def test_known_api_spend_limit_blocks(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_known_api_usd": 0.0})
    t = submit_task(app, p["id"], title="billed", objective="x",
                    metadata={"mock": {"files": MUL_IMPL, "billed_cost_usd": 0.5}})
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "BLOCKED" and t["error"] == "spending limit"
    assert app.approvals.list(task_id=t["id"], status="pending")[0]["kind"] == "spending"


async def test_concurrency_limit(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo, limits={"max_concurrency": 1})
    tids = [submit_task(app, p["id"], title=f"t{i}", objective="x",
                        metadata={"mock": {"files": {f"f{i}.txt": "x"}, "sleep_s": 0.3}})["id"] for i in range(2)]
    seen_max = 0

    async def watch():
        nonlocal seen_max
        while True:
            seen_max = max(seen_max, len(app.engine.running))
            await asyncio.sleep(0.02)

    w = asyncio.create_task(watch())
    await drive(app, tids)
    w.cancel()
    assert seen_max == 1


async def test_read_only_independent_proposals(mock_app, tmp_path):
    app = mock_app
    repo = make_repo(tmp_path / "ro", {"AGENTS.md": "x", "notes.md": "research notes"})
    p = app.projects.import_folder(str(repo), config={"permissions": {"mode": "read_only"}})
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    with pytest.raises(PermissionError):
        submit_task(app, p["id"], title="x", objective="y", workflow="plan_implement_test_review")
    t = submit_task(app, p["id"], title="Next milestone", objective="Propose next milestone",
                    workflow="independent_proposals")
    await drive(app, [t["id"]])
    t = app.tasks.get(t["id"])
    assert t["state"] == "COMPLETED"
    props = app.artifacts.list(task_id=t["id"], kind="proposal")
    assert len(props) == 2
    models = {r["model"] for r in runs(app, t["id"]) if r["kind"] == "agent"}
    assert len(models) == 2, "independent proposers must be different models"
    cmp_ = app.artifacts.list(task_id=t["id"], kind="comparison")[0]["data"]
    assert len(cmp_["pairs"]) == 1
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert before == after
    assert t["worktree_path"] is None


async def test_decomposition_into_dependent_subtasks(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="Goal", objective="Big goal", as_milestone=True,
                    metadata={"decompose": True, "mock": {
                        "files": {"step.txt": "x"},
                        "subtasks": [{"title": "First", "objective": "do first", "depends_on": []},
                                     {"title": "Second", "objective": "do second", "depends_on": [0]}]}},
                    approval_policy="never_merge")
    await drive(app, None, timeout=90)
    parent = app.tasks.get(t["id"])
    kids = app.tasks.list(parent_id=t["id"])
    assert len(kids) == 2
    second = next(k for k in kids if k["title"] == "Second")
    first = next(k for k in kids if k["title"] == "First")
    assert second["depends_on"] == [first["id"]]
    assert all(k["state"] == "COMPLETED" for k in kids)
    assert parent["state"] == "COMPLETED"
    assert {m["status"] for m in app.projects.milestones(p["id"])} == {"done"}


async def test_every_phase_approval_policy(mock_app, py_repo):
    app = mock_app
    p = project(app, py_repo)
    t = submit_task(app, p["id"], title="gated", objective="x", approval_policy="every_phase",
                    metadata={"mock": {"files": MUL_IMPL}})
    await drive(app, [t["id"]])
    assert app.tasks.get(t["id"])["state"] == "AWAITING_APPROVAL"
    ap = app.approvals.list(task_id=t["id"], status="pending")[0]
    assert ap["kind"] == "phase"
    app.engine.apply_approval(ap["id"], True)
    await drive(app, [t["id"]])
    assert runs(app, t["id"])[0]["phase"] == "plan"
    assert app.approvals.list(task_id=t["id"], status="pending")[0]["payload"]["phase_key"].startswith("implement")


async def test_single_engine_lock(mock_app):
    a = mock_app.engine
    b = Engine(mock_app)
    assert a.acquire_lock() is True
    assert b.acquire_lock() is False
    a.release_lock()
    assert b.acquire_lock() is True
    b.release_lock()
