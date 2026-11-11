"""In-memory fake of the GitHub REST endpoints the bridge uses (test double).

Served through httpx.MockTransport so the real GitHubClient code (headers,
ETags, rate-limit tracking, error mapping, retries) is exercised. Used by the
test suite and by scripts/demo_bridge.py. It never talks to the network.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .github import GitHubClient


def _ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FakeRepo:
    full_name: str
    private: bool = True
    permissions: dict = field(default_factory=lambda: {"admin": True, "push": True, "triage": True, "pull": True})
    issues: dict[int, dict] = field(default_factory=dict)
    comments: dict[int, dict] = field(default_factory=dict)
    commits: set[str] = field(default_factory=set)


class FakeGitHub:
    def __init__(self, viewer: str = "forge-bot", token: str = "test-token"):
        self.viewer = viewer
        self.token = token
        self.repos: dict[str, FakeRepo] = {}
        self._ids = itertools.count(1000)
        self.clock = time.time
        self.fail_next: list[Any] = []       # e.g. [503, "network", 401, 429]
        self.rate_remaining = 5000
        self.calls: list[tuple[str, str]] = []

    # ------------------------------------------------------------ fixtures
    def add_repo(self, full_name: str, **kw: Any) -> FakeRepo:
        self.repos[full_name] = FakeRepo(full_name, **kw)
        return self.repos[full_name]

    def add_issue(self, repo: str, *, author: str, title: str, body: str, labels: tuple[str, ...] = ("forge-task",),
                  association: str = "OWNER", is_pr: bool = False) -> dict:
        r = self.repos[repo]
        n = max(r.issues, default=0) + 1
        t = self.clock()
        issue = {"id": next(self._ids), "number": n, "title": title, "body": body, "state": "open",
                 "user": {"login": author}, "author_association": association,
                 "labels": [{"name": l} for l in labels], "html_url": f"https://github.com/{repo}/issues/{n}",  # noqa: E741
                 "created_at": _ts(t), "updated_at": _ts(t)}
        if is_pr:
            issue["pull_request"] = {"url": "x"}
        r.issues[n] = issue
        return issue

    def edit_issue(self, repo: str, number: int, *, body: str) -> None:
        i = self.repos[repo].issues[number]
        i["body"], i["updated_at"] = body, _ts(self.clock() + 1)

    def add_comment(self, repo: str, number: int, *, author: str, body: str, association: str = "OWNER") -> dict:
        t = self.clock()
        c = {"id": next(self._ids), "issue_number": number, "body": body, "user": {"login": author},
             "author_association": association, "created_at": _ts(t), "updated_at": _ts(t)}
        self.repos[repo].comments[c["id"]] = c
        self.repos[repo].issues[number]["updated_at"] = _ts(t)
        return c

    def comments_on(self, repo: str, number: int, *, by: str | None = None) -> list[dict]:
        return [c for c in self.repos[repo].comments.values()
                if c["issue_number"] == number and (by is None or c["user"]["login"] == by)]

    def client(self, **kw: Any) -> GitHubClient:
        return GitHubClient(token=self.token, base_url="https://api.github.test",
                            transport=httpx.MockTransport(self.handle), sleep=lambda s: None, **kw)

    # ------------------------------------------------------------ HTTP handler
    def _json(self, status: int, data: Any, request: httpx.Request) -> httpx.Response:
        body = json.dumps(data).encode()
        etag = '"' + hashlib.sha1(body).hexdigest() + '"'
        headers = {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": str(self.rate_remaining),
                   "x-ratelimit-reset": str(int(self.clock()) + 3600), "etag": etag,
                   "content-type": "application/json"}
        if status == 200 and request.method == "GET" and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(status, content=body, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail_next:
            f = self.fail_next.pop(0)
            if f == "network":
                raise httpx.ConnectError("simulated network failure", request=request)
            headers = {"x-ratelimit-remaining": "0" if f == 429 else str(self.rate_remaining),
                       "x-ratelimit-reset": str(int(self.clock()) + 120)}
            if f == 429:
                headers["retry-after"] = "120"
            return httpx.Response(int(f), json={"message": "simulated"}, headers=headers)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        self.rate_remaining -= 1
        path, m = request.url.path, request.method
        params = dict(request.url.params)
        if path == "/user":
            return self._json(200, {"login": self.viewer, "id": 1}, request)
        mm = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if not mm or mm.group(1) not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = self.repos[mm.group(1)], mm.group(2) or ""
        if rest == "" and m == "GET":
            return self._json(200, {"full_name": repo.full_name, "private": repo.private,
                                    "visibility": "private" if repo.private else "public",
                                    "permissions": repo.permissions, "has_issues": True}, request)
        if rest == "/issues" and m == "GET":
            label, since = params.get("labels"), params.get("since")
            out = [i for i in repo.issues.values() if i["state"] == params.get("state", "open")
                   and (not label or any(l["name"] == label for l in i["labels"]))  # noqa: E741
                   and (not since or i["updated_at"] >= since)]
            return self._json(200, sorted(out, key=lambda i: i["updated_at"]), request)
        cm = re.match(r"^/issues/(\d+)/comments$", rest)
        if cm and m == "GET":
            n, since = int(cm.group(1)), params.get("since")
            out = [c for c in repo.comments.values() if c["issue_number"] == n
                   and (not since or c["updated_at"] >= since)]
            return self._json(200, sorted(out, key=lambda c: c["id"]), request)
        if cm and m == "POST":
            if not repo.permissions.get("pull"):
                return httpx.Response(403, json={"message": "Resource not accessible"})
            body = json.loads(request.content)["body"]
            c = self.add_comment(repo.full_name, int(cm.group(1)), author=self.viewer, body=body,
                                 association="NONE")
            return self._json(201, c, request)
        um = re.match(r"^/issues/comments/(\d+)$", rest)
        if um and m == "PATCH":
            c = repo.comments.get(int(um.group(1)))
            if not c:
                return httpx.Response(404, json={"message": "Not Found"})
            c["body"], c["updated_at"] = json.loads(request.content)["body"], _ts(self.clock())
            return self._json(200, c, request)
        km = re.match(r"^/commits/([0-9a-f]{7,40})$", rest)
        if km and m == "GET":
            return self._json(200, {"sha": km.group(1)}, request) if km.group(1) in repo.commits else \
                httpx.Response(404, json={"message": "No commit found"})
        return httpx.Response(404, json={"message": "Not Found"})
