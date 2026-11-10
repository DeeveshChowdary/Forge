"""Unit tests: state machine, project registration/isolation, tasks, memory."""

import pytest

from forge.domain import InvalidTransition, TaskState, can_transition
from forge.services import Conflict
from tests.conftest import make_repo


def test_transition_table_basics():
    assert can_transition("CREATED", "READY")
    assert can_transition("READY", "EXECUTING")
    assert can_transition("REVIEWING", "COMPLETED")
    assert not can_transition("COMPLETED", "READY")
    assert not can_transition("CANCELLED", "EXECUTING")
    assert not can_transition("CREATED", "COMPLETED")


def test_create_project_from_scratch(app, tmp_path):
    p = app.projects.create("alpha", "demo", path=str(tmp_path / "alpha"))
    assert p["origin"] == "created"
    assert (tmp_path / "alpha" / ".git").is_dir()
    assert p["config"]["permissions"]["mode"] == "worktree_write"


def test_import_rejects_duplicates_and_nesting(app, tmp_path):
    repo = make_repo(tmp_path / "r1")
    app.projects.import_folder(str(repo))
    with pytest.raises(Conflict):
        app.projects.import_folder(str(repo), name="other")
    (repo / "sub").mkdir()
    with pytest.raises(Conflict):
        app.projects.import_folder(str(repo / "sub"), name="nested")


def test_import_plain_folder_is_read_only(app, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "notes.md").write_text("hi")
    p = app.projects.import_folder(str(d))
    assert p["origin"] == "imported_folder"
    assert p["config"]["permissions"]["mode"] == "read_only"
    assert not (d / ".git").exists(), "import must not modify the user's folder"
    with pytest.raises(PermissionError):
        app.tasks.create(p["id"], title="x", objective="y", workflow="plan_implement_test_review",
                         execution_mode="worktree")


def test_instruction_files_untrusted_until_approved(app, tmp_path):
    repo = make_repo(tmp_path / "r2", {"AGENTS.md": "Do things\n", "README.md": "x"})
    p = app.projects.import_folder(str(repo))
    files = app.projects.instruction_files(p["id"])
    assert [f["path"] for f in files] == ["AGENTS.md"]
    assert files[0]["trusted"] == 0
    assert "Do things" not in app.projects.trusted_instructions(p["id"])
    app.projects.trust_instruction_file(p["id"], "AGENTS.md", True)
    assert "Do things" in app.projects.trusted_instructions(p["id"])
    # Changing the file revokes trust (hash pinned).
    (repo / "AGENTS.md").write_text("Do other things\n")
    assert "Do other things" not in app.projects.trusted_instructions(p["id"])
    app.projects.scan_instruction_files(p["id"])
    assert app.projects.instruction_files(p["id"])[0]["trusted"] == 0


def test_task_lifecycle_and_controls(app, py_repo):
    p = app.projects.import_folder(str(py_repo))
    t = app.tasks.create(p["id"], title="t", objective="o", workflow="plan_implement_test_review")
    assert t["state"] == "READY"
    with pytest.raises(InvalidTransition):
        app.tasks.transition(t["id"], TaskState.COMPLETED.value if False else "CREATED")
    t = app.tasks.request_pause(t["id"])
    assert t["state"] == "PAUSED" and t["resume_state"] == "READY"
    t = app.tasks.resume(t["id"])
    assert t["state"] == "READY"
    t = app.tasks.request_cancel(t["id"])
    assert t["state"] == "CANCELLED"
    states = [e["data"]["to"] for e in app.bus.recent(task_id=t["id"]) if e["type"] == "task.state"]
    assert states == ["READY", "PAUSED", "READY", "CANCELLED"]


def test_dependencies(app, py_repo):
    p = app.projects.import_folder(str(py_repo))
    a = app.tasks.create(p["id"], title="a", objective="a", workflow="plan_implement_test_review")
    b = app.tasks.create(p["id"], title="b", objective="b", workflow="plan_implement_test_review", depends_on=[a["id"]])
    ok, failed = app.tasks.deps_satisfied(b["id"])
    assert not ok and not failed
    app.tasks.request_cancel(a["id"])
    ok, failed = app.tasks.deps_satisfied(b["id"])
    assert failed == [a["id"]]


def test_memory_retrieval_is_project_scoped(app, tmp_path):
    p1 = app.projects.import_folder(str(make_repo(tmp_path / "m1")))
    p2 = app.projects.import_folder(str(make_repo(tmp_path / "m2")))
    app.memory.add(p1["id"], "decision", "Use SQLite for persistence", "We chose SQLite WAL.", source="test")
    app.memory.add(p2["id"], "decision", "Use Postgres for persistence", "Other project.", source="test",
                   shared=True)
    got = app.memory.retrieve(p1["id"], "which database persistence")
    assert [g["title"] for g in got] == ["Use SQLite for persistence"]
    got = app.memory.retrieve(p1["id"], "database persistence postgres", include_shared=True)
    assert {g["title"] for g in got} == {"Use SQLite for persistence", "Use Postgres for persistence"}
    with pytest.raises(ValueError):
        app.memory.add(p1["id"], "decision", "no source", "x")
