"""Forge command-line interface.

Uses the same services, database and engine as the web UI. If a `forge serve`
engine is running, commands enqueue work for it; otherwise `forge run` drives
the engine in the foreground.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from . import workflows as wfmod
from .app import Forge
from .config import DEFAULT_HOST, DEFAULT_PORT, WEB_DIST
from .engine import submit_task
from .router import RouteContext, RoutingError

C = {"r": "\033[31m", "g": "\033[32m", "y": "\033[33m", "b": "\033[34m", "d": "\033[2m", "x": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def ok(b: bool) -> str:
    return f"{C['g']}✓{C['x']}" if b else f"{C['r']}✗{C['x']}"


def ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "-"


def _forge() -> Forge:
    return Forge.create(discover=False)


# ------------------------------------------------------------------ doctor
def cmd_doctor(a) -> int:
    f = _forge()
    print(f"Forge {__version__}  home={f.paths.home}")
    checks = []

    def which(n):
        return shutil.which(n)

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            return None

    checks.append(("Python ≥3.12", sys.version_info >= (3, 12), sys.version.split()[0]))
    g = ver(["git", "--version"])
    checks.append(("git", bool(g), g))
    wt = subprocess.run(["git", "worktree", "-h"], capture_output=True, text=True).returncode in (0, 129)
    checks.append(("git worktree support", wt, ""))
    checks.append(("node", bool(which("node")), ver(["node", "--version"])))
    checks.append(("pnpm (web UI build)", bool(which("pnpm")), ver(["pnpm", "--version"])))
    checks.append(("uv", bool(which("uv") or Path.home().joinpath(".local/bin/uv").exists()), ""))
    checks.append(("web UI built", (WEB_DIST / "index.html").exists(), str(WEB_DIST)))
    checks.append(("database", f.paths.db.exists(), str(f.paths.db)))
    for name, good, detail in checks:
        print(f"  {ok(good)} {name:24} {C['d']}{detail or ''}{C['x']}")
    print("\nProviders:")
    statuses = f.registry.refresh(probe=False)
    if a.probe:
        asyncio.run(f.registry.probe_all(only_unverified=False))
        statuses = f.registry.statuses()
    for s in statuses:
        print(f"  {ok(s.get('installed'))} {s['display_name']:34} {s.get('version') or ''}")
        print(f"      path: {s.get('path')}")
        print(f"      auth: {s.get('auth_state')} — {s.get('auth_detail')}  billing: {s.get('billing')}")
        if s.get("last_probe_error"):
            print(f"      {C['y']}last probe error: {s['last_probe_error'][:200]}{C['x']}")
        for n in s.get("notes", []):
            print(f"      note: {n}")
        for m in s.get("models", []):
            mark = {"verified": C["g"] + "verified" + C["x"], "failed": C["r"] + "failed" + C["x"]}.get(m["status"], m["status"])
            res = f" → {m['resolved_id']}" if m.get("resolved_id") and m["resolved_id"] != m["model_id"] else ""
            hidden = " (disabled)" if not m["enabled"] else ""
            print(f"      - {m['model_id']:20} {mark}{res}{hidden} {C['d']}tier={m['profile'].get('tier')}{C['x']}")
    if not a.probe:
        print(f"\n{C['d']}Models are 'unverified' until probed: run `forge doctor --probe` "
              f"(sends one tiny prompt per enabled model using your subscriptions).{C['x']}")
    return 0


# ------------------------------------------------------------------ serve
def cmd_serve(a) -> int:
    import uvicorn

    from .api import create_app
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.i_understand_remote:
        print("Refusing to bind to a non-local address without --i-understand-remote (the API has no auth).")
        return 2
    f = Forge.create(discover=True)
    app = create_app(f, run_engine=not a.no_engine)
    print(f"Forge UI/API on http://{a.host}:{a.port}  (home {f.paths.home})")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


# ------------------------------------------------------------------ projects
def cmd_project_add(a) -> int:
    f = _forge()
    cfg: dict = {}
    if a.test_command:
        cfg["test_command"] = a.test_command
    if a.read_only:
        cfg.setdefault("permissions", {})["mode"] = "read_only"
    if a.create:
        p = f.projects.create(a.name or Path(a.target).name, a.description, a.target, cfg)
    elif Path(a.target).expanduser().exists():
        p = f.projects.import_folder(a.target, a.name, a.description, cfg)
    else:
        p = f.projects.import_git(a.target, a.name, a.description, cfg)
    print(f"{ok(True)} registered {p['name']} ({p['id']}) at {p['root_path']} "
          f"[{p['origin']}, mode={p['config']['permissions']['mode']}]")
    files = f.projects.instruction_files(p["id"])
    if files:
        print(f"  {C['y']}untrusted instruction files: {', '.join(x['path'] for x in files)} — approve them in the UI "
              f"(Project → Settings) if agents should follow them{C['x']}")
    return 0


def cmd_project_list(a) -> int:
    f = _forge()
    rows = f.projects.list()
    if a.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    if not rows:
        print("no projects. add one: forge project add <path>")
    for p in rows:
        counts = " ".join(f"{k}:{v}" for k, v in sorted(p["task_counts"].items()))
        print(f"{p['id']}  {p['name']:24} {p['status']:7} {p['config']['permissions']['mode']:14} {p['root_path']}  {C['d']}{counts}{C['x']}")
    return 0


# ------------------------------------------------------------------ plan / run
def _overrides(specs: list[str] | None) -> dict:
    out: dict = {}
    for s in specs or []:
        phase, _, rest = s.partition("=")
        parts = rest.split(":")
        if len(parts) < 2:
            raise SystemExit(f"bad --route {s!r}; expected phase=provider:model[:effort]")
        route = {"provider": parts[0], "model": parts[1], **({"effort": parts[2]} if len(parts) > 2 else {})}
        if phase in out:  # repeated phase = several agents (fan-out phases such as 'propose')
            prev = out[phase]
            out[phase] = [*(prev if isinstance(prev, list) else [prev]), route]
        else:
            out[phase] = route
    return out


def cmd_plan(a) -> int:
    f = _forge()
    p = f.projects.get(a.project)
    cfg = f.projects.config(p["id"])
    wf = wfmod.get(a.workflow or cfg.default_workflow)
    ov = _overrides(a.route)
    print(f"Dry-run plan for {p['name']} — workflow {wf.name} (mode {a.mode}); nothing is executed.\n")
    for ph in wf.phases:
        if ph.kind in ("test", "compare"):
            what = cfg.test_command or "(no test command configured)" if ph.kind == "test" else "deterministic comparison"
            print(f"  {ph.key:12} {ph.state.value:10} {ph.kind:7} {what}")
            continue
        try:
            o = ov.get(ph.key)
            d = f.router.route(RouteContext(project_id=p["id"], task_id="plan-preview", phase=ph.key, role=ph.role,
                                            complexity=a.complexity, mode=a.mode,
                                            override=o[0] if isinstance(o, list) else o), persist=False)
            print(f"  {ph.key:12} {ph.state.value:10} {ph.role:12} → {d.provider}/{d.model} effort={d.effort}")
            for r in d.reasons:
                print(f"      {C['d']}{r}{C['x']}")
        except RoutingError as e:
            print(f"  {ph.key:12} {ph.state.value:10} {ph.role:12} → {C['r']}unroutable: {e}{C['x']}")
    if wf.fix_phase:
        print(f"\n  review/test failures loop through '{wf.fix_phase.key}' at most "
              f"{cfg.limits.max_review_cycles} time(s)")
    return 0


def cmd_run(a) -> int:
    f = _forge()
    meta = {"decompose": True} if a.decompose else {}
    t = submit_task(f, a.project, title=a.title or a.objective[:80], objective=a.objective, workflow=a.workflow,
                    routing_mode=a.mode, routing_overrides=_overrides(a.route), complexity=a.complexity,
                    review_depth=a.review, approval_policy=a.approval, metadata=meta,
                    acceptance_criteria=a.accept or [], execution_mode="read_only" if a.read_only else None)
    print(f"{ok(True)} task {t['id']} queued ({t['workflow']})")
    lock = f.engine.lock_info()
    if lock and lock["alive"] and not a.foreground:
        print(f"  the Forge server engine (pid {lock['pid']}) will execute it. Follow with: forge logs {t['id']} -f")
        return 0
    if a.detach:
        print("  no engine running; start one with `forge serve` to execute queued tasks")
        return 0
    print(f"  {C['d']}no server engine running — executing in the foreground (Ctrl-C to stop; the task resumes later){C['x']}")
    return _follow(f, t["id"], drive=True)


def _print_event(e: dict) -> None:
    lvl = {"error": C["r"], "warn": C["y"]}.get(e["level"], "")
    print(f"{C['d']}{ts(e['ts'])}{C['x']} {lvl}{e['type']:22}{C['x']} {e['message']}")


def _follow(f: Forge, tid: str, *, drive: bool) -> int:
    async def main():
        last = 0
        for e in f.bus.recent(task_id=tid, limit=50):
            _print_event(e)
            last = e["id"]
        runner = asyncio.create_task(f.engine.run_until_idle([tid], timeout=6 * 3600)) if drive else None
        try:
            while True:
                for e in f.bus.since(last, task_id=tid):
                    _print_event(e)
                    last = e["id"]
                t = f.tasks.get(tid)
                if runner and runner.done():
                    runner.result()
                    break
                if not runner and t["state"] in ("COMPLETED", "FAILED", "CANCELLED", "AWAITING_APPROVAL", "PAUSED",
                                                 "BLOCKED"):
                    break
                await asyncio.sleep(0.4)
        finally:
            if runner and not runner.done():
                f.engine.stop()
                runner.cancel()
                await f.engine.shutdown()
            elif drive:
                f.engine.release_lock()
        for e in f.bus.since(last, task_id=tid):
            _print_event(e)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted; the task will be recovered and resumed by the next engine start")
    t = f.tasks.get(tid)
    print(f"\ntask {tid}: {t['state']}" + (f" — {t['disposition']}" if t["disposition"] else ""))
    for ap in f.approvals.list(task_id=tid, status="pending"):
        print(f"  {C['y']}approval needed [{ap['kind']}] {ap['title']}: forge approve {ap['id']}  |  forge reject {ap['id']}{C['x']}")
    return 0 if t["state"] in ("COMPLETED", "AWAITING_APPROVAL") else 1


# ------------------------------------------------------------------ status / control
def cmd_status(a) -> int:
    f = _forge()
    if a.task:
        t = f.tasks.get(a.task)
        print(f"{t['id']}  {t['title']}\n  state={t['state']} phase={t['phase']} workflow={t['workflow']} "
              f"mode={t['routing_mode']} cycle={t['review_cycle']}")
        if t["branch"]:
            print(f"  worktree={t['worktree_path']} branch={t['branch']} base={t['base_commit'][:10]}")
        if t["disposition"]:
            print(f"  disposition: {t['disposition']}")
        if t["error"]:
            print(f"  {C['r']}error: {t['error'][:500]}{C['x']}")
        for r in f.db.query("SELECT * FROM runs WHERE task_id=? ORDER BY started_at", (t["id"],)):
            dur = f"{(r['ended_at'] or time.time()) - r['started_at']:.0f}s"
            who = f"{r['provider']}/{r['resolved_model'] or r['model']}" if r["provider"] else "(test runner)"
            print(f"   - {r['phase']:10} {r['role']:12} {who:40} {r['state']:11} {dur}")
        for ap in f.approvals.list(task_id=t["id"], status="pending"):
            print(f"  {C['y']}pending approval {ap['id']} [{ap['kind']}]: {ap['title']}{C['x']}")
        return 0
    lock = f.engine.lock_info()
    print(f"engine: {'running (pid %s)' % lock['pid'] if lock and lock['alive'] else 'not running'}")
    for t in f.tasks.list(a.project, limit=a.limit):
        print(f"{t['id']}  {t['state']:17} {(t['phase'] or ''):10} {t['title'][:60]}")
    return 0


def cmd_control(a) -> int:
    f = _forge()
    fn = {"pause": f.tasks.request_pause, "resume": f.tasks.resume, "cancel": f.tasks.request_cancel,
          "retry": f.tasks.retry}[a.cmd]
    t = fn(a.task)
    print(f"{t['id']}: {t['state']}" + (" (pause requested)" if t["pause_requested"] else "")
          + (" (cancel requested)" if t["cancel_requested"] else ""))
    return 0


def cmd_worker(a) -> int:
    """Run the execution engine in the foreground (no web server)."""
    f = Forge.create(discover=True)
    lock = f.engine.lock_info()
    if lock and lock["alive"]:
        print(f"another engine (pid {lock['pid']}) is already running; it will execute queued tasks")
        return 1
    if a.task:
        return _follow(f, a.task, drive=True)
    print("engine running; Ctrl-C to stop (running tasks are requeued and resume next time)")
    try:
        if a.until_idle:
            asyncio.run(f.engine.run_until_idle(None, timeout=24 * 3600))
            f.engine.release_lock()
        else:
            asyncio.run(f.engine.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_revise(a) -> int:
    f = _forge()
    t = f.engine.request_revision(a.task, a.note or "")
    print(f"{t['id']}: {t['state']} (phase {t['phase']}, cycle {t['review_cycle']}) — the engine will run the fix phase")
    return 0


def cmd_logs(a) -> int:
    f = _forge()
    t = f.tasks.get(a.task)
    if a.follow:
        return _follow(f, t["id"], drive=False)
    for e in f.bus.recent(task_id=t["id"], limit=a.n):
        _print_event(e)
    return 0


def cmd_decide(a) -> int:
    f = _forge()
    r = f.engine.apply_approval(a.approval, a.cmd == "approve", a.note or "")
    print(f"{r['id']}: {r['status']}")
    if r["task_id"]:
        t = f.tasks.get(r["task_id"])
        print(f"task {t['id']}: {t['state']}" + (f" — {t['disposition']}" if t["disposition"] else ""))
    return 0


def cmd_bridge(a) -> int:
    from .coordination.bridge import Bridge
    from .coordination.github import GitHubClient, GitHubError, resolve_token
    f = _forge()
    b = Bridge(f)
    if a.bcmd == "status":
        print(json.dumps(b.status(), indent=1, default=str))
    elif a.bcmd in ("enable", "disable"):
        cfg = b.save_config({"enabled": a.bcmd == "enable"})
        print(f"bridge {'enabled' if cfg.enabled else 'disabled'}; repositories: {', '.join(cfg.repos)}")
    elif a.bcmd == "check":
        cfg = b.config()
        token, src = resolve_token(cfg.token_source)
        print(f"token source: {src}")
        if not token:
            return 1
        c = GitHubClient(token=token)
        try:
            print(f"authenticated as: {c.viewer()['login']}")
            for repo in cfg.repos:
                info = c.repo(repo)
                print(f"{repo}: private={info['private']} permissions={info['permissions']}")
                c.list_issues(repo, label=cfg.repos[repo].label)
                print(f"  can list issues: yes (rate remaining {c.rate.remaining})")
        except GitHubError as e:
            print(f"{C['r']}GitHub check failed:{C['x']} {e}")
            return 1
        print("note: comment-write permission is only proven by the first published comment")
    elif a.bcmd == "sync":
        print(json.dumps(b.sync_once(), indent=1, default=str))
    elif a.bcmd == "run":
        print("bridge worker running; Ctrl-C to stop")
        try:
            asyncio.run(b.run_forever())
        except KeyboardInterrupt:
            pass
    return 0


def cmd_token(a) -> int:
    from .coordination.tokens import TokenService
    f = _forge()
    ts = TokenService(f.db)
    if a.tcmd == "create":
        projects = ["*"] if a.project == ["*"] else [f.projects.get(x)["id"] for x in a.project]
        t = ts.create(a.name, a.scope, projects)
        print(f"token id {t['id']} scopes={t['scopes']} projects={t['projects']}")
        print(f"token (shown once, store it securely): {t['token']}")
    elif a.tcmd == "list":
        for t in ts.list():
            print(f"{t['id']}  {t['name']:20} {t['prefix']}…  scopes={t['scopes']} projects={t['projects']}"
                  f"{'  REVOKED' if t['revoked_at'] else ''}")
    elif a.tcmd == "revoke":
        ts.revoke(a.id)
        print(f"revoked {a.id}")
    return 0


def cmd_mcp(a) -> int:
    from .coordination.mcp import MCPServer
    token = os.environ.get("FORGE_COORDINATION_TOKEN")
    if not token:
        print("set FORGE_COORDINATION_TOKEN (create one with `forge token create`)", file=sys.stderr)
        return 2
    from .coordination.tokens import AuthError
    try:
        server = MCPServer(_forge(), token)
    except AuthError as e:
        print(f"forge mcp: {e}", file=sys.stderr)
        return 2
    server.serve_stdio()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="forge", description="Forge: local-first multi-model AI engineering harness")
    ap.add_argument("--version", action="version", version=f"forge {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check environment, providers and models")
    d.add_argument("--probe", action="store_true", help="verify each enabled model with a tiny real prompt")
    d.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("serve", help="run the API, web UI and execution engine")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--no-engine", action="store_true", help="serve UI/API only; do not execute tasks")
    s.add_argument("--i-understand-remote", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_serve)

    p = sub.add_parser("project", help="manage projects")
    psub = p.add_subparsers(dest="pcmd", required=True)
    pa = psub.add_parser("add", help="import a folder/repo (or --create a new one)")
    pa.add_argument("target", help="local path, git URL, or (with --create) the new project path")
    pa.add_argument("--name")
    pa.add_argument("--description", default="")
    pa.add_argument("--create", action="store_true")
    pa.add_argument("--read-only", action="store_true")
    pa.add_argument("--test-command", help='e.g. "{python} -m pytest -q" or "npm test"')
    pa.set_defaults(fn=cmd_project_add)
    pl = psub.add_parser("list")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(fn=cmd_project_list)

    pp = sub.add_parser("plan", help="dry-run: show phases and routing decisions without executing")
    pp.add_argument("project")
    pp.add_argument("--workflow", choices=sorted(wfmod.WORKFLOWS))
    pp.add_argument("--mode", default="balanced", choices=["balanced", "max_quality", "economy", "manual"])
    pp.add_argument("--complexity", default="medium", choices=["low", "medium", "high"])
    pp.add_argument("--route", action="append", help="phase=provider:model[:effort] (repeatable)")
    pp.set_defaults(fn=cmd_plan)

    r = sub.add_parser("run", help="submit a goal; executes in the foreground if no server engine is running")
    r.add_argument("project")
    r.add_argument("objective")
    r.add_argument("--title")
    r.add_argument("--workflow", choices=sorted(wfmod.WORKFLOWS))
    r.add_argument("--mode", choices=["balanced", "max_quality", "economy", "manual"])
    r.add_argument("--complexity", default="medium", choices=["low", "medium", "high"])
    r.add_argument("--route", action="append", help="phase=provider:model[:effort] (repeatable)")
    r.add_argument("--review", default="standard", choices=["none", "standard", "independent"])
    r.add_argument("--approval", default="risky", choices=["risky", "every_phase", "never_merge"])
    r.add_argument("--accept", action="append", help="acceptance criterion (repeatable)")
    r.add_argument("--decompose", action="store_true", help="let the planner split the goal into dependent subtasks")
    r.add_argument("--read-only", action="store_true")
    r.add_argument("--foreground", action="store_true", help="execute here even if a server engine is running")
    r.add_argument("--detach", action="store_true", help="only enqueue")
    r.set_defaults(fn=cmd_run)

    st = sub.add_parser("status", help="list tasks or show one task")
    st.add_argument("task", nargs="?")
    st.add_argument("--project")
    st.add_argument("--limit", type=int, default=30)
    st.set_defaults(fn=cmd_status)

    for name in ("pause", "resume", "cancel", "retry"):
        c = sub.add_parser(name, help=f"{name} a task")
        c.add_argument("task")
        c.set_defaults(fn=cmd_control)

    wk = sub.add_parser("worker", help="run the execution engine in the foreground without the web server")
    wk.add_argument("task", nargs="?", help="follow this task and stop when it is done or needs you")
    wk.add_argument("--until-idle", action="store_true")
    wk.set_defaults(fn=cmd_worker)

    rv = sub.add_parser("revise", help="send the latest review + a note back to the implementer (from AWAITING_APPROVAL)")
    rv.add_argument("task")
    rv.add_argument("--note")
    rv.set_defaults(fn=cmd_revise)

    lg = sub.add_parser("logs", help="show a task's event log")
    lg.add_argument("task")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", type=int, default=200)
    lg.set_defaults(fn=cmd_logs)

    for name in ("approve", "reject"):
        c = sub.add_parser(name, help=f"{name} a pending approval")
        c.add_argument("approval")
        c.add_argument("--note")
        c.set_defaults(fn=cmd_decide)

    br = sub.add_parser("bridge", help="GitHub coordination bridge")
    br.add_argument("bcmd", choices=["status", "check", "enable", "disable", "sync", "run"])
    br.set_defaults(fn=cmd_bridge)

    tk = sub.add_parser("token", help="coordination API tokens for external assistants")
    tsub = tk.add_subparsers(dest="tcmd", required=True)
    tc = tsub.add_parser("create")
    tc.add_argument("name")
    tc.add_argument("--scope", action="append", choices=["read", "tasks:write", "feedback:write"], default=None)
    tc.add_argument("--project", action="append", required=True, help="project name/id (repeatable) or '*'")
    tsub.add_parser("list")
    tr = tsub.add_parser("revoke")
    tr.add_argument("id")
    tk.set_defaults(fn=cmd_token)

    mc = sub.add_parser("mcp", help="stdio MCP server for local MCP clients (needs FORGE_COORDINATION_TOKEN)")
    mc.set_defaults(fn=cmd_mcp)

    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except (KeyError, ValueError, PermissionError) as e:
        print(f"{C['r']}error:{C['x']} {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("NotFound", "Conflict", "InvalidTransition", "RoutingError"):
            print(f"{C['r']}error:{C['x']} {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
