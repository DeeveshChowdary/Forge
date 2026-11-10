"""API and CLI tests (shared state, guards, end-to-end task flow through HTTP)."""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from forge.api import create_app
from forge.cli import main as cli_main
from tests.test_engine import MUL_IMPL, TEST_CMD

H = {"X-Forge-Client": "test"}


@pytest.fixture
async def client(mock_app):
    app = create_app(mock_app, run_engine=False)
    with TestClient(app) as c:
        yield c, mock_app


async def test_guards(client):
    c, _ = client
    assert c.get("/api/health").status_code == 200
    assert c.post("/api/projects", json={"mode": "create", "name": "x"}).status_code == 403  # no custom header
    r = c.post("/api/projects", json={"mode": "create", "name": "x"},
               headers={**H, "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.get("/api/health", headers={"Host": "evil.example"}).status_code == 403


async def test_project_task_flow_over_http(client, py_repo):
    c, app = client
    r = c.post("/api/projects", headers=H, json={"mode": "import_folder", "path": str(py_repo),
                                                 "config": {"test_command": TEST_CMD}})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert c.post("/api/projects", headers=H, json={"mode": "import_folder", "path": str(py_repo)}).status_code == 409
    assert c.get(f"/api/projects/{pid}/file", params={"path": "../../etc/passwd"}).status_code == 403
    assert c.get(f"/api/projects/{pid}/file", params={"path": "calc.py"}).json()["content"].startswith("def add")
    assert any(f["name"] == "calc.py" for f in c.get(f"/api/projects/{pid}/files").json())
    r = c.post("/api/tasks", headers=H, json={"project_id": pid, "title": "mul", "objective": "Add mul",
                                              "mock": {"files": MUL_IMPL}})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    await app.engine.run_until_idle([tid], timeout=60)
    t = c.get(f"/api/tasks/{tid}").json()
    assert t["state"] == "AWAITING_APPROVAL"
    assert [x["phase"] for x in t["runs"]] == ["plan", "implement", "test", "review"]
    assert all(x["is_mock"] for x in t["runs"] if x["kind"] == "agent")
    assert t["routing"] and t["routing"][0]["reasons"]
    assert "def mul" in c.get(f"/api/tasks/{tid}/diff").text
    ap = c.get("/api/approvals").json()[0]
    r = c.post(f"/api/approvals/{ap['id']}", headers=H, json={"approve": True})
    assert r.json()["status"] == "approved"
    assert c.get(f"/api/tasks/{tid}").json()["state"] == "COMPLETED"
    ov = c.get("/api/overview").json()
    assert ov["recent"][0]["id"] == tid
    usage = c.get("/api/usage").json()["per_provider"]
    assert usage[0]["provider"] == "mock" and usage[0]["billed_usd"] == 0
    ev = c.get("/api/events", params={"task_id": tid}).json()
    assert any(e["type"] == "task.state" for e in ev)


async def test_mock_scenarios_require_mock_enabled(app, py_repo):
    a = create_app(app, run_engine=False)
    with TestClient(a) as c:
        pid = c.post("/api/projects", headers=H, json={"mode": "import_folder", "path": str(py_repo)}).json()["id"]
        r = c.post("/api/tasks", headers=H, json={"project_id": pid, "title": "x", "objective": "y", "mock": {}})
        assert r.status_code == 403


async def test_providers_models_and_preview(client, py_repo):
    c, app = client
    pid = c.post("/api/projects", headers=H, json={"mode": "import_folder", "path": str(py_repo)}).json()["id"]
    provs = c.get("/api/providers").json()["providers"]
    assert [p["provider"] for p in provs] == ["mock"]
    assert all(m["status"] == "verified" for m in provs[0]["models"])
    r = c.patch("/api/models", headers=H, params={"provider": "mock", "model_id": "mock-fast"},
                json={"profile": {"tier": "standard"}, "preference": 0.5})
    assert r.json()["profile"]["source"] == "user"
    d = c.post("/api/router/preview", headers=H, json={"project_id": pid, "complexity": "high"}).json()
    assert d["model"] == "mock-strong" and d["reasons"]


async def test_integrations_and_memory_api(client, py_repo):
    c, app = client
    pid = c.post("/api/projects", headers=H, json={"mode": "import_folder", "path": str(py_repo)}).json()["id"]
    ints = {i["type"]: i for i in c.get(f"/api/projects/{pid}/integrations").json()}
    assert ints["git"]["enabled"] and ints["git"]["status"]["ok"]
    assert not ints["zotero"]["enabled"]
    r = c.put(f"/api/projects/{pid}/integrations/zotero", headers=H,
              json={"enabled": True, "config": {"backend": "local_api", "base_url": "http://example.com/api/"}})
    z = {i["type"]: i for i in r.json()}["zotero"]
    assert z["status"]["ok"] is False  # non-local URL refused
    r = c.post(f"/api/projects/{pid}/memory", headers=H, json={"kind": "requirement", "title": "Use SI units",
                                                               "content": "All values in SI"})
    assert r.status_code == 200
    assert c.get(f"/api/projects/{pid}/memory", params={"q": "units"}).json()[0]["title"] == "Use SI units"


def test_cli_end_to_end(forge_home, py_repo, capsys):
    from forge.app import Forge
    f = Forge.create(discover=False)
    f.save_settings({"enable_mock_provider": True, "disabled_providers": ["claude_code", "codex"]})
    f.registry.refresh()
    import asyncio
    asyncio.run(f.registry.probe_all())
    f.close()
    assert cli_main(["project", "add", str(py_repo), "--test-command", TEST_CMD]) == 0
    assert cli_main(["project", "list", "--json"]) == 0
    out = capsys.readouterr().out
    pid = json.loads(out[out.index("[\n"):])[0]["id"]
    assert cli_main(["plan", pid]) == 0
    assert "mock/" in capsys.readouterr().out
    assert cli_main(["run", pid, "Add a README note", "--approval", "never_merge"]) == 0
    out = capsys.readouterr().out
    assert "COMPLETED" in out or "AWAITING_APPROVAL" in out
    tid = out.split("task ")[1].split()[0]
    assert cli_main(["status", tid]) == 0
    assert "plan" in capsys.readouterr().out
    assert cli_main(["logs", tid, "-n", "5"]) == 0
    assert cli_main(["cancel", tid]) == 0  # terminal task: no-op, not an error


def test_cli_entrypoint_installed():
    out = subprocess.run([sys.executable, "-m", "forge.cli", "--version"], capture_output=True, text=True)
    assert out.stdout.startswith("forge ")
