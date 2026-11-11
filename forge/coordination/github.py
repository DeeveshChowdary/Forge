"""Minimal GitHub REST client for the coordination bridge.

* Official REST API (api.github.com, version 2022-11-28) over httpx.
* Token sources, in order for `auto`: FORGE_GITHUB_TOKEN env var, `gh auth token`
  (if the GitHub CLI is installed and logged in), macOS keychain item
  `forge-github-token`. The token is never logged, stored by Forge, or returned.
* Conditional requests (ETag / If-None-Match): unchanged polls return 304 and do
  not count against the rate limit.
* Rate-limit headers are tracked; requests stop before the reserve is reached.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

API = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubError(Exception):
    kind = "error"


class GitHubAuthError(GitHubError):
    kind = "auth"


class GitHubPermissionError(GitHubError):
    kind = "permission"


class GitHubNotFound(GitHubError):
    kind = "not_found"


class GitHubRateLimited(GitHubError):
    kind = "rate_limited"

    def __init__(self, msg: str, reset_at: float):
        super().__init__(msg)
        self.reset_at = reset_at


class GitHubNetworkError(GitHubError):
    kind = "network"


def resolve_token(source: str = "auto") -> tuple[str | None, str]:
    """Returns (token, description of source). Never raises; never logs the token."""
    if source in ("auto", "env"):
        t = os.environ.get("FORGE_GITHUB_TOKEN")
        if t:
            return t.strip(), "env:FORGE_GITHUB_TOKEN"
        if source == "env":
            return None, "env:FORGE_GITHUB_TOKEN not set"
    if source in ("auto", "gh"):
        gh = shutil.which("gh") or ("/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else None)
        if gh:
            try:
                p = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
                if p.returncode == 0 and p.stdout.strip():
                    return p.stdout.strip(), "gh auth token"
            except Exception:  # noqa: BLE001
                pass
        if source == "gh":
            return None, "gh CLI not installed or not logged in"
    if source in ("auto", "keychain"):
        try:
            p = subprocess.run(["security", "find-generic-password", "-s", "forge-github-token", "-w"],
                               capture_output=True, text=True, timeout=15)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip(), "macOS keychain:forge-github-token"
        except Exception:  # noqa: BLE001
            pass
    return None, ("no GitHub token found (set FORGE_GITHUB_TOKEN, run `gh auth login`, or store one with "
                  "`security add-generic-password -s forge-github-token -a forge -w`)")


@dataclass
class RateState:
    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None
    requests: int = 0
    not_modified: int = 0


@dataclass
class GitHubClient:
    token: str
    base_url: str = API
    transport: httpx.BaseTransport | None = None
    timeout: float = 20.0
    reserve: int = 50                                   # stop before the quota is exhausted
    sleep: Callable[[float], None] = time.sleep
    max_retries: int = 2
    rate: RateState = field(default_factory=RateState)
    _etags: dict[str, tuple[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.base_url, transport=self.transport, timeout=self.timeout, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION, "User-Agent": "forge-coordination-bridge"})

    def __repr__(self) -> str:  # never print the token
        return f"GitHubClient(base_url={self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ transport
    def _track(self, r: httpx.Response) -> None:
        h = r.headers
        if "x-ratelimit-remaining" in h:
            self.rate.remaining = int(h["x-ratelimit-remaining"])
            self.rate.limit = int(h.get("x-ratelimit-limit", 0) or 0)
            self.rate.reset_at = float(h.get("x-ratelimit-reset", 0) or 0)

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                conditional: bool = False) -> Any:
        if self.rate.remaining is not None and self.rate.remaining <= self.reserve and \
                (self.rate.reset_at or 0) > time.time():
            raise GitHubRateLimited(f"GitHub rate-limit reserve reached ({self.rate.remaining} left)",
                                    self.rate.reset_at or time.time() + 60)
        key = f"{path}?{sorted((params or {}).items())}"
        headers = {}
        if conditional and key in self._etags:
            headers["If-None-Match"] = self._etags[key][0]
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.HTTPError as e:
                if attempt <= self.max_retries:
                    self.sleep(min(2 ** attempt, 10))
                    continue
                raise GitHubNetworkError(f"network error talking to GitHub: {type(e).__name__}") from None
            self.rate.requests += 1
            self._track(r)
            if r.status_code >= 500 and attempt <= self.max_retries:
                self.sleep(min(2 ** attempt, 10))
                continue
            break
        if r.status_code == 304 and key in self._etags:
            self.rate.not_modified += 1
            return self._etags[key][1]
        if r.status_code == 401:
            raise GitHubAuthError("GitHub rejected the token (401)")
        if r.status_code in (403, 429):
            retry_after = r.headers.get("retry-after")
            if r.status_code == 429 or retry_after or self.rate.remaining == 0:
                reset = time.time() + float(retry_after) if retry_after else (self.rate.reset_at or time.time() + 60)
                raise GitHubRateLimited(f"GitHub rate limited ({r.status_code})", reset)
            raise GitHubPermissionError(f"GitHub denied {method} {path} (403): token lacks permission")
        if r.status_code == 404:
            raise GitHubNotFound(f"{method} {path}: not found (or no access)")
        if r.status_code >= 500:
            raise GitHubNetworkError(f"GitHub server error {r.status_code}")
        if r.status_code >= 400:
            raise GitHubError(f"GitHub error {r.status_code} on {method} {path}")
        data = r.json() if r.content else None
        if conditional and r.headers.get("etag"):
            self._etags[key] = (r.headers["etag"], data)
        return data

    # ------------------------------------------------------------ endpoints
    def viewer(self) -> dict:
        u = self.request("GET", "/user")
        return {"login": u["login"], "id": u["id"]}

    def repo(self, full_name: str) -> dict:
        r = self.request("GET", f"/repos/{full_name}")
        return {"full_name": r["full_name"], "private": r["private"], "visibility": r.get("visibility"),
                "permissions": r.get("permissions") or {}, "has_issues": r.get("has_issues", True)}

    def list_issues(self, full_name: str, *, label: str, since: str | None = None) -> list[dict]:
        params = {"labels": label, "state": "open", "per_page": 50, "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        items = self.request("GET", f"/repos/{full_name}/issues", params=params, conditional=True) or []
        return [i for i in items if "pull_request" not in i]

    def list_comments(self, full_name: str, number: int, *, since: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"per_page": 100}
        if since:
            params["since"] = since
        return self.request("GET", f"/repos/{full_name}/issues/{number}/comments", params=params,
                            conditional=True) or []

    def create_comment(self, full_name: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body})

    def update_comment(self, full_name: str, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"/repos/{full_name}/issues/comments/{comment_id}", json={"body": body})

    def commit_exists(self, full_name: str, sha: str) -> bool:
        try:
            self.request("GET", f"/repos/{full_name}/commits/{sha}")
            return True
        except (GitHubNotFound, GitHubError):
            return False
