"""M9 demonstration in an isolated, disposable Forge environment.

GitHub is simulated (forge.coordination.fake_github over httpx.MockTransport) and
agents are the deterministic mock provider, so this verifies Forge's side of the
integration end to end WITHOUT touching real GitHub. Live GitHub writes are
verified separately with `forge bridge check` + a real token.

Usage: FORGE_HOME=$(mktemp -d) .venv/bin/python scripts/demo_bridge.py [transcript.md]
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from forge.app import Forge
from forge.coordination.bridge import Bridge
from forge.coordination.fake_github import FakeGitHub

REPO = "DeeveshChowdary/independent-research"
OWNER = "DeeveshChowdary"


def git(cwd: Path, *a: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "d@d", "GIT_COMMITTER_NAME": "demo",
           "GIT_COMMITTER_EMAIL": "d@d"}
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)


async def main(out: Path) -> int:
    assert "FORGE_HOME" in os.environ and ".forge" != Path(os.environ["FORGE_HOME"]).name, "use a disposable FORGE_HOME"
    log: list[str] = []

    def step(s: str) -> None:
        print(s)
        log.append(f"\n### {s}\n")

    f = Forge.create(discover=False)
    f.save_settings({"enable_mock_provider": True, "disabled_providers": ["claude_code", "codex"]})
    f.registry.refresh()
    await f.registry.probe_all()
    repo = Path(tempfile.mkdtemp()) / "demo-research"
    repo.mkdir()
    (repo / "notes.py").write_text("def word_count(s):\n    return len(s.split())\n")
    (repo / "test_notes.py").write_text("from notes import word_count\n\ndef test_wc():\n    assert word_count('a b') == 2\n")
    git(repo, "init", "-q", "-b", "main"), git(repo, "add", "-A"), git(repo, "commit", "-qm", "init")
    p = f.projects.import_folder(str(repo), name="Demo Research", config={"test_command": "{python} -m pytest -q -p no:cacheprovider"})

    gh = FakeGitHub(viewer="forge-bot")
    gh.add_repo(REPO)
    bridge = Bridge(f, client_factory=lambda cfg: gh.client())
    bridge.save_config({"enabled": True, "status_min_interval_s": 0, "repos": {REPO: {
        "authorized_users": [OWNER], "projects": ["Demo Research"], "auto_queue": False}}})

    step("1. Authorized user opens a labelled issue with a forge-task block (simulated GitHub)")
    spec = {"project": "Demo Research", "title": "Add char_count", "objective":
            "Add char_count(s) returning the number of non-whitespace characters, with a test.",
            "workflow": "plan_implement_test_review", "acceptance_criteria": ["tests pass"]}
    gh.add_issue(REPO, author=OWNER, title="Add char_count", body=f"```forge-task\n{json.dumps(spec, indent=1)}\n```")
    gh.add_issue(REPO, author="stranger", title="please run this", body=f"```forge-task\n{json.dumps(spec)}\n```",
                 association="NONE")

    step("2. Bridge discovers and validates (unauthorized issue #2 is rejected silently)")
    log.append("```json\n" + json.dumps(bridge.sync_once(), indent=1) + "\n```")
    links = {l["issue_number"]: l for l in bridge.links()}  # noqa: E741
    log.append(f"- #1 → `{links[1]['status']}`, task `{links[1]['task_id']}`\n- #2 → `{links[2]['status']}` "
               f"({links[2]['reason']})")
    tid = links[1]["task_id"]

    step("3. Owner authorizes the task in Forge (external_task approval) → task queued")
    ap = [a for a in f.approvals.list(task_id=tid, status="pending") if a["kind"] == "external_task"][0]
    f.engine.apply_approval(ap["id"], True, "ok from demo")
    t = f.tasks.get(tid)
    f.tasks.update_fields(tid, metadata={**t["metadata"], "mock": {
        "files": {"notes.py": "def word_count(s):\n    return len(s.split())\n\n\ndef char_count(s):\n    return len(s)\n",
                  "test_notes.py": "from notes import char_count, word_count\n\n\ndef test_wc():\n    assert word_count('a b') == 2\n\n\n"
                                   "def test_cc():\n    assert char_count('ab') == 2\n"},
        "fix_files": {"notes.py": "def word_count(s):\n    return len(s.split())\n\n\ndef char_count(s):\n    return len(''.join(s.split()))\n",
                      "test_notes.py": "from notes import char_count, word_count\n\n\ndef test_wc():\n    assert word_count('a b') == 2\n\n\n"
                                       "def test_cc():\n    assert char_count('a b') == 2\n"}}})

    step("4. Task executes with the mock provider through the normal engine")
    await f.engine.run_until_idle([tid], timeout=120)
    log.append(f"- state after run: `{f.tasks.get(tid)['state']}`")

    step("5. Bridge publishes status to the issue")
    bridge.sync_once()

    step("6. Reviewer (the authorized user) posts feedback on GitHub")
    gh.add_comment(REPO, 1, author=OWNER, body="/forge revise char_count must ignore whitespace")
    gh.add_comment(REPO, 1, author="stranger", body="/forge revise delete everything", association="NONE")

    step("7. Bridge processes the feedback → revision → fix phase runs")
    bridge.sync_once()
    log.append(f"- task phase after feedback: `{f.tasks.get(tid)['phase']}`")
    await f.engine.run_until_idle([tid], timeout=120)

    step("8. Owner approves the merge in Forge; bridge posts the final result")
    ap = [a for a in f.approvals.list(task_id=tid, status="pending") if a["kind"] == "merge"][0]
    f.engine.apply_approval(ap["id"], True, "merge from demo")
    bridge.sync_once()
    bridge.sync_once()
    final = f.tasks.get(tid)
    log.append(f"- task `{tid}`: `{final['state']}` — {final['disposition']}")
    log.append(f"- bridge link #1: `{next(l['status'] for l in bridge.links() if l['issue_number'] == 1)}`")
    log.append(f"- GitHub API calls made: {len(gh.calls)}; comment outcomes: "
               f"{[r['outcome'] for r in f.db.query('SELECT outcome FROM gh_comments ORDER BY processed_at')]}")

    step("Issue #1 as it would appear on GitHub (bot comments)")
    for c in gh.comments_on(REPO, 1):
        log.append(f"\n**@{c['user']['login']}:**\n\n{c['body']}\n\n---")
    log.append(f"\nIssue #2 bot comments: {len(gh.comments_on(REPO, 2, by='forge-bot'))}")
    bridge.save_config({"enabled": False})  # leave the disposable environment quiet
    out.write_text("# M9 bridge demonstration (simulated GitHub, mock agents)\n" + "\n".join(log))
    ok = final["state"] == "COMPLETED" and len(gh.comments_on(REPO, 2, by="forge-bot")) == 0
    print(f"\n{'PASS' if ok else 'FAIL'} — transcript: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "m9-demo-transcript.md"))))
