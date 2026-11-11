"""GitHub coordination bridge (polling).

Inbound: open issues carrying the configured label in allowlisted repositories,
written by allowlisted authors (who must also be repo owner/member/collaborator)
and containing exactly one fenced ```forge-task JSON block, become Forge tasks
through CoordinationService.submit (the normal engine path). Slash commands in
comments from authorized users (`/forge revise …`, `/forge feedback …`,
`/forge status`) become feedback. Everything else in issues and comments is
ignored — nothing from GitHub is ever executed as a command or shell input.

Outbound: one status comment per issue, edited in place when the task changes,
plus milestone comments (authorization needed, approvals, blockers, final
summary) delivered through a deduplicated outbox. All text is sanitized.

Idempotency: (repo, issue_number) is unique and is inserted in the same
transaction that creates the task; processed comment ids are stored in the same
transaction as the action they trigger; outbox messages have unique keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..db import dumps, loads, new_id, now
from ..domain import TaskState
from .github import (GitHubAuthError, GitHubClient, GitHubError, GitHubRateLimited, resolve_token)
from .sanitize import clean_inbound, sanitize
from .service import (CoordinationError, CoordinationService, ExternalTaskRequest, Feedback, iso)
from .tokens import Principal

if TYPE_CHECKING:
    from ..app import Forge

DEFAULT_REPO = "DeeveshChowdary/independent-research"
MARKER = "<!-- forge-bridge -->"
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
FINAL = {"completed", "rejected", "failed"}
LOCK_NAME = "github_bridge"
LOCK_STALE_S = 300
_BLOCK = re.compile(r"```forge-task[ \t]*\n(.*?)\n```", re.S)
_COMMAND = re.compile(r"\A/forge[ \t]+(revise|feedback|status)\b[ \t]*(.*)\Z", re.S | re.I)
_REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class RepoConfig(BaseModel):
    enabled: bool = True
    label: str = "forge-task"
    authorized_users: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)      # Forge project names this repo may target
    auto_queue: bool = False                                # False: tasks wait for authorization in Forge
    require_private: bool = True


class BridgeConfig(BaseModel):
    enabled: bool = False
    poll_interval_s: int = Field(120, ge=30, le=3600)
    token_source: Literal["auto", "env", "gh", "keychain"] = "auto"
    status_min_interval_s: int = Field(60, ge=0, le=3600)
    repos: dict[str, RepoConfig] = Field(default_factory=lambda: {
        DEFAULT_REPO: RepoConfig(authorized_users=["DeeveshChowdary"], projects=["Independent Research"])})

    @field_validator("repos")
    @classmethod
    def _check(cls, v: dict) -> dict:
        for name in v:
            if not _REPO.match(name):
                raise ValueError(f"invalid repository name {name!r} (expected owner/name)")
        return v


def parse_request(body: str) -> tuple[ExternalTaskRequest | None, str | None]:
    blocks = _BLOCK.findall(body or "")
    if not blocks:
        return None, "no ```forge-task JSON block found"
    if len(blocks) > 1:
        return None, "more than one ```forge-task block; submit one task per issue"
    try:
        data = json.loads(blocks[0])
    except ValueError as e:
        return None, f"the forge-task block is not valid JSON ({e.msg} at line {e.lineno})"
    if not isinstance(data, dict):
        return None, "the forge-task block must be a JSON object"
    try:
        return ExternalTaskRequest.model_validate(data), None
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:6])
        return None, f"schema errors: {errs}"


def _h(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class Bridge:
    def __init__(self, forge: "Forge", client_factory: Callable[[BridgeConfig], GitHubClient] | None = None,
                 clock: Callable[[], float] = time.time):
        self.forge = forge
        self.db = forge.db
        self.coord = CoordinationService(forge)
        self.client_factory = client_factory or self._default_client
        self.clock = clock
        self.owner = new_id("bridge")
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- config/state
    def config(self) -> BridgeConfig:
        return BridgeConfig.model_validate(self.db.get_setting("github_bridge", {}) or {})

    def save_config(self, patch: dict) -> BridgeConfig:
        cur = self.config().model_dump()
        if "repos" in patch:
            cur["repos"] = patch.pop("repos")
        cfg = BridgeConfig.model_validate({**cur, **patch})
        self.db.set_setting("github_bridge", cfg.model_dump())
        self.forge.bus.emit("coordination.bridge_config", f"GitHub bridge {'enabled' if cfg.enabled else 'disabled'}"
                            f"; repositories: {', '.join(cfg.repos) or 'none'}")
        return cfg

    def state(self) -> dict:
        return self.db.get_setting("github_bridge_state", {}) or {}

    def _save_state(self, st: dict) -> None:
        self.db.set_setting("github_bridge_state", st)

    @staticmethod
    def _default_client(cfg: BridgeConfig) -> GitHubClient:
        token, source = resolve_token(cfg.token_source)
        if not token:
            raise GitHubAuthError(source)
        return GitHubClient(token=token)

    # ------------------------------------------------------------- lock
    def _lock(self) -> bool:
        t = self.clock()
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM worker_locks WHERE name=?", (LOCK_NAME,)).fetchone()
            if r is None or r["owner"] == self.owner or t - r["heartbeat"] > LOCK_STALE_S:
                c.execute("INSERT INTO worker_locks(name,owner,pid,heartbeat) VALUES (?,?,?,?) ON CONFLICT(name) DO "
                          "UPDATE SET owner=excluded.owner, pid=excluded.pid, heartbeat=excluded.heartbeat",
                          (LOCK_NAME, self.owner, os.getpid(), t))
                return True
            return False

    def _unlock(self) -> None:
        self.db.execute("DELETE FROM worker_locks WHERE name=? AND owner=?", (LOCK_NAME, self.owner))

    # ------------------------------------------------------------- status for UI
    def status(self) -> dict:
        cfg, st = self.config(), self.state()
        links = self.links()
        counts: dict[str, int] = {}
        for l in links:  # noqa: E741
            counts[l["status"]] = counts.get(l["status"], 0) + 1
        out = self.db.query("SELECT status, COUNT(*) n FROM gh_outbox GROUP BY status")
        _, token_src = resolve_token(cfg.token_source) if cfg.enabled else (None, "not checked (bridge disabled)")
        lock = self.db.one("SELECT * FROM worker_locks WHERE name=?", (LOCK_NAME,))
        return {"config": cfg.model_dump(), "state": st, "link_counts": counts,
                "outbox": {r["status"]: r["n"] for r in out}, "token_source": token_src,
                "worker": ({"pid": lock["pid"], "heartbeat": lock["heartbeat"],
                            "alive": self.clock() - lock["heartbeat"] < max(LOCK_STALE_S, 3 * cfg.poll_interval_s)}
                           if lock else None)}

    def links(self, limit: int = 200) -> list[dict]:
        rows = self.db.query("SELECT * FROM gh_links ORDER BY updated_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["task_state"] = None
            if r["task_id"]:
                t = self.db.one("SELECT state, phase FROM tasks WHERE id=?", (r["task_id"],))
                if t:
                    r["task_state"], r["task_phase"] = t["state"], t["phase"]
        return rows

    # ------------------------------------------------------------- main cycle
    def sync_once(self) -> dict:
        cfg = self.config()
        summary: dict[str, Any] = {"discovered": 0, "queued": 0, "awaiting_authorization": 0, "rejected": 0,
                                   "comments_processed": 0, "status_updates": 0, "messages_sent": 0, "errors": []}
        if not cfg.enabled:
            return {"skipped": "bridge disabled", **summary}
        st = self.state()
        if (st.get("backoff_until") or 0) > self.clock():
            return {"skipped": f"backing off until {iso(st['backoff_until'])}", **summary}
        if not self._lock():
            return {"skipped": "another bridge worker holds the lock", **summary}
        st["last_attempt_at"] = self.clock()
        client = None
        try:
            client = self.client_factory(cfg)
            if not st.get("viewer") or self.clock() - (st.get("viewer_checked_at") or 0) > 3600:
                st["viewer"] = client.viewer()["login"]
                st["viewer_checked_at"] = self.clock()
            for repo, rc in cfg.repos.items():
                if rc.enabled:
                    self._sync_repo(client, repo, rc, st, cfg, summary)
            self._flush_outbox(client, summary)
            st.update(last_success_at=self.clock(), last_error=None, last_error_kind=None, consecutive_failures=0,
                      backoff_until=None)
        except GitHubRateLimited as e:
            st.update(last_error=str(e), last_error_kind=e.kind, backoff_until=e.reset_at)
            summary["errors"].append(str(e))
        except GitHubError as e:
            n = int(st.get("consecutive_failures") or 0) + 1
            st.update(last_error=str(e), last_error_kind=e.kind, consecutive_failures=n,
                      backoff_until=self.clock() + min(cfg.poll_interval_s * 2 ** n, 1800))
            summary["errors"].append(str(e))
        finally:
            if client is not None:
                st["rate"] = {"remaining": client.rate.remaining, "limit": client.rate.limit,
                              "reset_at": client.rate.reset_at, "requests_last_cycle": client.rate.requests,
                              "not_modified_last_cycle": client.rate.not_modified}
            st["last_sync_at"] = self.clock()
            st["last_summary"] = {k: v for k, v in summary.items() if k != "errors"}
            self._save_state(st)
            self._unlock()
        if summary["errors"]:
            self.forge.bus.emit("coordination.bridge_error", f"GitHub bridge sync failed: {summary['errors'][0]}",
                                level="warn")
        return summary

    def _sync_repo(self, client: GitHubClient, repo: str, rc: RepoConfig, st: dict, cfg: BridgeConfig,
                   summary: dict) -> None:
        checks = st.setdefault("repo_checks", {})
        chk = checks.get(repo) or {}
        if self.clock() - (chk.get("checked_at") or 0) > 600:
            info = client.repo(repo)
            chk = {"checked_at": self.clock(), "private": info["private"], "visibility": info["visibility"],
                   "permissions": info["permissions"], "has_issues": info["has_issues"]}
            perms = info["permissions"]
            chk["can_comment"] = bool(perms.get("triage") or perms.get("push") or perms.get("admin")
                                      or perms.get("maintain") or (perms.get("pull") and info["private"]))
            chk["ok"] = not (rc.require_private and not info["private"])
            chk["error"] = None if chk["ok"] else "repository is not private; the bridge refuses to process it"
            checks[repo] = chk
        if not chk.get("ok"):
            summary["errors"].append(f"{repo}: {chk.get('error')}")
            return
        since = (st.setdefault("since", {})).get(repo)
        issues = client.list_issues(repo, label=rc.label, since=since)
        latest = since
        for issue in issues:
            self._inbound_issue(repo, rc, issue, st.get("viewer"), summary)
            if not latest or issue["updated_at"] > latest:
                latest = issue["updated_at"]
        st["since"][repo] = latest
        for link in self.db.query("SELECT * FROM gh_links WHERE repo=? AND task_id IS NOT NULL AND status NOT IN "
                                  "('completed','rejected','failed')", (repo,)):
            comments = client.list_comments(repo, link["issue_number"], since=link["comments_since"])
            newest = link["comments_since"]
            for c in comments:
                self._inbound_comment(repo, rc, link, c, st.get("viewer"), summary)
                if not newest or c["updated_at"] > newest:
                    newest = c["updated_at"]
            if newest != link["comments_since"]:
                self.db.execute("UPDATE gh_links SET comments_since=? WHERE id=?", (newest, link["id"]))
        for link in self.db.query("SELECT * FROM gh_links WHERE repo=? AND task_id IS NOT NULL", (repo,)):
            if link["status"] in FINAL and link["last_published"].startswith("final:"):
                continue
            if (link["next_retry_at"] or 0) > self.clock():
                continue
            try:
                self._publish(client, repo, link, cfg, summary)
                if link["retries"]:
                    self.db.execute("UPDATE gh_links SET retries=0, next_retry_at=NULL, error=NULL WHERE id=?",
                                    (link["id"],))
            except GitHubRateLimited:
                raise
            except GitHubError as e:
                n = link["retries"] + 1
                self.db.execute("UPDATE gh_links SET retries=?, next_retry_at=?, error=? WHERE id=?",
                                (n, self.clock() + min(60 * 2 ** n, 3600), str(e)[:300], link["id"]))
                summary["errors"].append(f"{repo}#{link['issue_number']}: {e}")

    # ------------------------------------------------------------- inbound
    def _authorized(self, rc: RepoConfig, login: str, association: str | None) -> bool:
        return login.lower() in {u.lower() for u in rc.authorized_users} and \
            (association or "").upper() in TRUSTED_ASSOCIATIONS

    def _link(self, repo: str, number: int) -> dict | None:
        return self.db.one("SELECT * FROM gh_links WHERE repo=? AND issue_number=?", (repo, number))

    def _set(self, lid: str, **f: Any) -> None:
        f["updated_at"] = self.clock()
        self.db.execute(f"UPDATE gh_links SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), lid))

    def _inbound_issue(self, repo: str, rc: RepoConfig, issue: dict, viewer: str | None, summary: dict) -> None:
        number, author = issue["number"], (issue.get("user") or {}).get("login", "")
        title, body = clean_inbound(issue.get("title"), 300), clean_inbound(issue.get("body"), 20000)
        h = _h(title, body)
        link = self._link(repo, number)
        authorized = self._authorized(rc, author, issue.get("author_association"))
        if link:
            if link["task_id"] and h != link["request_hash"] and authorized:
                self._enqueue(link["id"], f"edited:{h[:16]}",
                              "Edits to the issue after submission are not applied to the running Forge task. "
                              "Use `/forge feedback …` or `/forge revise …`, or open a new issue for new work.")
                self._set(link["id"], request_hash=h)
            if not (link["status"] == "rejected" and link["reason"].startswith("invalid") and authorized
                    and h != link["request_hash"]):
                return
            self.db.execute("DELETE FROM gh_links WHERE id=?", (link["id"],))   # corrected request: re-validate
        lid = new_id("gh")
        t = self.clock()
        try:
            self.db.execute(
                "INSERT INTO gh_links(id,repo,issue_number,issue_url,author,title,status,request_hash,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lid, repo, number, issue.get("html_url", ""), author, title, "discovered", h, t, t))
        except Exception:  # noqa: BLE001 - unique constraint: another worker took it
            return
        summary["discovered"] += 1
        self.forge.bus.emit("coordination.issue_discovered", f"GitHub {repo}#{number} discovered (author {author})",
                            data={"repo": repo, "issue": number})
        if not authorized:
            # No reply: do not engage with unauthorized authors.
            self._set(lid, status="rejected", reason="unauthorized author (not allowlisted or not a collaborator)")
            summary["rejected"] += 1
            return
        req, err = parse_request(body)
        if err:
            self._set(lid, status="rejected", reason=f"invalid request: {err}")
            self._enqueue(lid, f"invalid:{h[:16]}", f"**Not accepted:** {err}.\n\n{SCHEMA_HELP}")
            summary["rejected"] += 1
            return
        if req.project.lower() not in {p.lower() for p in rc.projects}:
            self._set(lid, status="rejected", reason=f"project {req.project!r} is not allowed for {repo}")
            self._enqueue(lid, f"invalid:{h[:16]}", f"**Not accepted:** project `{sanitize(req.project, max_len=100)}` "
                          f"is not enabled for this repository in Forge.")
            summary["rejected"] += 1
            return
        project_ids = []
        for name in rc.projects:
            try:
                project_ids.append(self.forge.projects.get(name)["id"])
            except Exception:  # noqa: BLE001
                pass
        principal = Principal("github", f"github:{author}", {"read", "tasks:write", "feedback:write"}, project_ids,
                              source="github")
        self._set(lid, status="validated")
        try:
            with self.db.tx():
                view = self.coord.submit(principal, req, source="github", source_ref=f"{repo}#{number}",
                                         require_authorization=not rc.auto_queue)
                status = "awaiting_authorization" if view["state"] == "AWAITING_APPROVAL" else "queued"
                self._set(lid, task_id=view["id"], project_id=view["project"]["id"], status=status)
        except CoordinationError as e:
            self._set(lid, status="rejected", reason=f"invalid request: {e}")
            self._enqueue(lid, f"invalid:{h[:16]}", f"**Not accepted:** {sanitize(str(e), max_len=500)}")
            summary["rejected"] += 1
            return
        summary[status] += 1

    def _inbound_comment(self, repo: str, rc: RepoConfig, link: dict, c: dict, viewer: str | None,
                         summary: dict) -> None:
        cid = c["id"]
        if self.db.one("SELECT 1 FROM gh_comments WHERE repo=? AND comment_id=?", (repo, cid)):
            return
        login = (c.get("user") or {}).get("login", "")
        body = clean_inbound(c.get("body"), 4000).strip()

        def record(outcome: str) -> None:
            self.db.execute("INSERT OR IGNORE INTO gh_comments(repo,comment_id,issue_number,author,outcome,"
                            "processed_at) VALUES (?,?,?,?,?,?)", (repo, cid, link["issue_number"], login, outcome,
                                                                   self.clock()))

        if (viewer and login == viewer) or MARKER in (c.get("body") or ""):
            return record("own")
        if not self._authorized(rc, login, c.get("author_association")):
            return record("ignored_unauthorized")
        m = _COMMAND.match(body)
        if not m:
            return record("no_command")
        cmd, rest = m.group(1).lower(), m.group(2).strip()
        principal = Principal("github", f"github:{login}", {"read", "feedback:write"}, [link["project_id"]],
                              source="github")
        reply = ""
        with self.db.tx():
            # The seen-marker and the action commit together: a crash cannot run the action twice.
            record(f"cmd:{cmd}")
            try:
                if cmd == "status":
                    self._set(link["id"], last_published="")
                    reply = ""
                elif not rest:
                    reply = f"`/forge {cmd}` needs a message after the command."
                else:
                    res = self.coord.feedback(principal, link["task_id"],
                                              Feedback(kind="revision" if cmd == "revise" else "comment",
                                                       message=rest), source="github")
                    reply = f"Accepted `/forge {cmd}`: {res['action']}."
            except CoordinationError as e:
                reply = f"`/forge {cmd}` not applied: {sanitize(str(e), max_len=400)}"
            except ValidationError:
                reply = f"`/forge {cmd}` not applied: message too long (max 4000 characters)."
            if reply:
                self._enqueue(link["id"], f"reply:{cid}", reply)
        summary["comments_processed"] += 1

    # ------------------------------------------------------------- outbound
    def _derive_status(self, task: dict) -> str:
        st = task["state"]
        if st == "AWAITING_APPROVAL" and any(a["kind"] == "external_task" for a in
                                             self.forge.approvals.list(task_id=task["id"], status="pending")):
            return "awaiting_authorization"
        if st == "COMPLETED":
            return "completed"
        if st == "CANCELLED" and "rejected by user (external" in (task["disposition"] or ""):
            return "rejected"
        if st in ("FAILED", "CANCELLED"):
            return "failed"
        if st in ("CREATED", "READY") and not task["started_at"]:
            return "queued"
        return "running"

    def _publish(self, client: GitHubClient, repo: str, link: dict, cfg: BridgeConfig, summary: dict) -> None:
        task = self.forge.tasks.get(link["task_id"])
        status = self._derive_status(task)
        view = self.coord.task_view(task, detail=True)
        roots = self.coord._roots(task)
        lid = link["id"]
        if status != link["status"]:
            self._set(lid, status=status)
            self.forge.bus.emit("coordination.link_status", f"{repo}#{link['issue_number']} → {status}",
                                project_id=task["project_id"], task_id=task["id"], data={"status": status})
        # Milestones (each published once, survives restarts).
        if status == "awaiting_authorization":
            self._enqueue(lid, "awaiting_authorization",
                          f"Forge received this request as task `{task['id']}`. It is **awaiting authorization** "
                          f"by the Forge owner before any agent runs.")
        for a in self.forge.approvals.list(task_id=task["id"], status="pending"):
            if a["kind"] != "external_task":
                self._enqueue(lid, f"approval:{a['id']}",
                              f"**Human approval required in Forge:** {sanitize(a['title'], roots=roots, max_len=200)}"
                              f" (`{a['kind']}`). This cannot be approved from GitHub.")
        if task["state"] == "BLOCKED":
            self._enqueue(lid, f"blocked:{_h(task['error'] or '')[:12]}",
                          f"**Blocked:** {sanitize(task['error'] or 'needs attention in Forge', roots=roots, max_len=600)}")
        body = MARKER + "\n" + render_status(repo, link, view, status, self._commit_links(client, repo, task, view))
        snap = _h(re.sub(r"_Updated .*?_", "", body))
        due = status != link["status"] or self.clock() - (link["last_published_at"] or 0) >= cfg.status_min_interval_s
        final = status in FINAL
        if snap != link["last_published"].removeprefix("final:") and (due or final):
            if link["status_comment_id"]:
                client.update_comment(repo, link["status_comment_id"], body)
            else:
                c = client.create_comment(repo, link["issue_number"], body)
                self._set(lid, status_comment_id=c["id"])
            self._set(lid, last_published=("final:" if final else "") + snap, last_published_at=self.clock())
            summary["status_updates"] += 1
        if final:
            self._enqueue(lid, "final", render_final(view, status, roots, self._highlights(task, roots)))
            if not link["last_published"].startswith("final:"):
                self._set(lid, last_published="final:" + snap, last_published_at=self.clock())

    def _highlights(self, task: dict, roots: list[str]) -> list[str]:
        """Key content of the latest plan/report/proposal artifacts (sanitized excerpts)."""
        out = []
        for kind in ("report", "proposal", "plan", "comparison"):
            a = self.forge.artifacts.latest(task["id"], kind)
            d = (a or {}).get("data") or {}
            if not d or kind == "comparison":
                continue
            part = [f"**{kind.title()}:** {sanitize(d.get('title') or d.get('summary', ''), roots=roots, max_len=600)}"]
            for key in ("findings", "steps", "next_steps", "risks"):
                items = d.get(key) or []
                if items:
                    part.append(f"_{key.replace('_', ' ')}:_")
                    part += [f"- {sanitize(x, roots=roots, max_len=300)}" for x in items[:6]]
            out.append("\n".join(part))
        return out

    def _commit_links(self, client: GitHubClient, repo: str, task: dict, view: dict) -> list[str]:
        out = []
        for c in view.get("commits", [])[:5]:
            pushed = client.commit_exists(repo, c["sha"])
            out.append(f"[`{c['short']}`](https://github.com/{repo}/commit/{c['sha']}) {c['subject']}" if pushed
                       else f"`{c['short']}` {c['subject']} _(local to Forge; not pushed)_")
        return out

    def _enqueue(self, link_id: str, key: str, body: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO gh_outbox(id,link_id,dedupe_key,body,created_at) VALUES (?,?,?,?,?)",
                        (new_id("ob"), link_id, f"{link_id}:{key}", MARKER + "\n" + sanitize(body, max_len=60000),
                         self.clock()))

    def _flush_outbox(self, client: GitHubClient, summary: dict) -> None:
        for m in self.db.query("SELECT o.*, l.repo, l.issue_number FROM gh_outbox o JOIN gh_links l ON l.id=o.link_id "
                               "WHERE o.status='pending' ORDER BY o.created_at LIMIT 30"):
            try:
                c = client.create_comment(m["repo"], m["issue_number"], m["body"])
                self.db.execute("UPDATE gh_outbox SET status='sent', sent_at=?, comment_id=?, attempts=attempts+1 "
                                "WHERE id=?", (self.clock(), c["id"], m["id"]))
                summary["messages_sent"] += 1
            except GitHubRateLimited:
                raise
            except GitHubError as e:
                failed = m["attempts"] + 1 >= 8
                self.db.execute("UPDATE gh_outbox SET attempts=attempts+1, last_error=?, status=? WHERE id=?",
                                (str(e)[:300], "failed" if failed else "pending", m["id"]))
                summary["errors"].append(f"outbox {m['dedupe_key']}: {e}")
                if isinstance(e, GitHubAuthError):
                    raise

    # ------------------------------------------------------------- worker loop
    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            cfg = self.config()
            if cfg.enabled:
                try:
                    await asyncio.to_thread(self.sync_once)
                except Exception as e:  # noqa: BLE001 - keep polling
                    self.forge.bus.emit("coordination.bridge_error", f"bridge cycle crashed: {e!r}", level="error")
            try:
                await asyncio.wait_for(self._stop.wait(), cfg.poll_interval_s if cfg.enabled else 30)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


SCHEMA_HELP = """To submit a task, add the `forge-task` label and put exactly one block like this in the issue body:

```forge-task
{
  "project": "Independent Research",
  "title": "Short title",
  "objective": "What to do, in plain language",
  "workflow": "research",
  "acceptance_criteria": ["…"],
  "complexity": "medium",
  "routing_mode": "balanced",
  "read_only": false
}
```"""


def _md(s: Any) -> str:
    return str(s if s is not None else "—").replace("|", "\\|").replace("\n", " ")


def render_status(repo: str, link: dict, v: dict, status: str, commits: list[str]) -> str:
    a = v.get("assigned") or {}
    agent = (f"{a.get('provider')}/{a.get('resolved_model') or a.get('model')}"
             + (f" (effort {a['effort']})" if a.get("effort") else "") + (" — running" if a.get("running") else "")
             if a else "not assigned yet")
    lines = [f"### Forge status: **{status.replace('_', ' ')}**", "",
             "| | |", "|---|---|",
             f"| Forge task | `{v['id']}` |", f"| Project | {_md(v['project']['name'])} |",
             f"| Workflow | `{v['workflow']}` |",
             f"| State | `{v['state']}`" + (f" · phase `{v['phase']}`" if v.get("phase") else "") + " |",
             f"| Agent | {_md(agent)} |",
             f"| Review cycle | {v['review_cycle']}/{_md(v.get('max_review_cycles'))} |"]
    if v.get("tests"):
        t = v["tests"]
        lines.append(f"| Tests | {'not configured' if t['skipped'] else ('passed' if t['passed'] else 'failed')}"
                     f" — {_md(t['summary'])} |")
    if v.get("review"):
        lines.append(f"| Review | {_md(v['review']['verdict'])} ({len(v['review']['issues'])} issue(s)) |")
    if v.get("pending_approvals"):
        lines.append("| Waiting for | " + "; ".join(_md(p["title"]) for p in v["pending_approvals"]) +
                     " (human approval in Forge) |")
    if v.get("error"):
        lines.append(f"| Error | {_md(v['error'])} |")
    if v.get("runs"):
        lines += ["", "<details><summary>Runs</summary>", "", "| Phase | Agent | State | Duration |",
                  "|---|---|---|---|"]
        for r in v["runs"][-15:]:
            who = f"{r['provider']}/{r['resolved_model'] or r['model']}" if r["provider"] else "test runner"
            lines.append(f"| {r['phase']} | {_md(who)} | {r['state']} | "
                         f"{(str(r['duration_s']) + 's') if r['duration_s'] is not None else '…'} |")
        lines += ["", "</details>"]
    if commits:
        lines += ["", "**Commits:** " + " · ".join(commits)]
    lines += ["", f"_Updated {v['updated_at']} UTC · edits in place · commands: `/forge status`, "
                  f"`/forge feedback <text>`, `/forge revise <text>`_"]
    return "\n".join(lines)


def render_final(v: dict, status: str, roots: list[str], highlights: list[str] | None = None) -> str:
    head = {"completed": "✅ Forge task completed", "failed": "❌ Forge task did not complete",
            "rejected": "⛔ Request rejected in Forge"}[status]
    lines = [f"## {head}", "", f"Task `{v['id']}` · state `{v['state']}`"]
    if v.get("disposition"):
        lines.append(f"\n**Outcome:** {v['disposition']}")
    if v.get("error"):
        lines.append(f"\n**Error:** {v['error']}")
    if v.get("tests"):
        t = v["tests"]
        lines.append(f"\n**Tests:** {'not configured' if t['skipped'] else ('passed' if t['passed'] else 'failed')}"
                     f" — {t['summary']}")
    if v.get("review"):
        r = v["review"]
        lines.append(f"\n**Review ({r['verdict']}):** {r['summary']}")
        for i in r["issues"][:10]:
            lines.append(f"- [{i['severity']}] {i['file']}: {i['description']}")
    for h in highlights or []:
        lines.append("\n" + h)
    lines.append("\nFull artifacts (plans, reports, reviews, diffs) are available through the Forge coordination "
                 f"API: `GET /api/v1/coordination/tasks/{v['id']}/artifacts`.")
    return "\n".join(lines)
