"""Git and worktree operations.

Rules enforced here:
* Agents only ever write inside a dedicated worktree on a `forge/<task>` branch.
* The user's main working tree is never modified except by an approved merge,
  and only when it has no uncommitted changes.
* Every git call uses an argv list (no shell).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Forge",
    "GIT_AUTHOR_EMAIL": "forge@localhost",
    "GIT_COMMITTER_NAME": "Forge",
    "GIT_COMMITTER_EMAIL": "forge@localhost",
}


class GitError(Exception):
    pass


def git(cwd: Path | str, *args: str, check: bool = True, timeout: int = 60, identity: bool = False) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if identity:
        env.update(GIT_ENV)
    p = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
    )
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def is_repo(path: Path | str) -> bool:
    try:
        return git(path, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
    except (FileNotFoundError, NotADirectoryError):
        return False


def repo_root(path: Path | str) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def has_commits(path: Path | str) -> bool:
    return git(path, "rev-parse", "--verify", "-q", "HEAD", check=False).strip() != ""


def head_commit(path: Path | str) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def current_branch(path: Path | str) -> str:
    return git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()


def dirty_files(path: Path | str) -> list[str]:
    out = git(path, "status", "--porcelain", "--untracked-files=all")
    return [line[3:] for line in out.splitlines() if line.strip()]


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")


JUNK = re.compile(r"(^|/)(__pycache__/|\.pytest_cache/|\.mypy_cache/|\.DS_Store$)|\.py[co]$")


def commit_all(path: Path | str, message: str) -> str | None:
    """Stage and commit everything in a *worktree*. Returns the new sha or None if no changes.

    Newly added build/cache artefacts (bytecode, pytest caches) are never committed;
    deletions of previously committed ones still are."""
    git(path, "add", "-A")
    added = git(path, "diff", "--cached", "--name-only", "--diff-filter=A").splitlines()
    junk = [f for f in added if JUNK.search(f)]
    if junk:
        git(path, "rm", "-q", "--cached", "--", *junk)
    if not git(path, "status", "--porcelain").strip():
        return None
    git(path, "commit", "-q", "-m", message, "--no-verify", identity=True)
    return head_commit(path)


@dataclass
class Worktree:
    path: Path
    branch: str
    base_commit: str


_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def create_worktree(repo: Path, dest_root: Path, task_id: str, base_ref: str = "HEAD") -> Worktree:
    if not has_commits(repo):
        raise GitError("repository has no commits; cannot create an isolated worktree")
    branch = f"forge/{_SAFE.sub('-', task_id)}"
    dest = dest_root / _SAFE.sub("-", task_id)
    base = git(repo, "rev-parse", base_ref).strip()
    if dest.exists():
        # Recovery path: reuse the worktree only if it is the one registered for this branch.
        listed = git(repo, "worktree", "list", "--porcelain")
        if f"worktree {dest.resolve()}" in listed and f"branch refs/heads/{branch}" in listed:
            return Worktree(dest, branch, base)
        raise GitError(f"worktree path {dest} exists but is not registered for {branch}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    exists = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).strip()
    if exists:
        git(repo, "worktree", "add", str(dest), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(dest), base)
    return Worktree(dest, branch, base)


def remove_worktree(repo: Path, path: Path, *, delete_branch: str | None = None) -> None:
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    if delete_branch:
        git(repo, "branch", "-D", delete_branch, check=False)


def diff(repo_or_wt: Path | str, base: str, ref: str = "HEAD", *, stat: bool = False) -> str:
    args = ["diff", "--no-color", "--no-ext-diff"]
    if stat:
        args.append("--stat")
    return git(repo_or_wt, *args, f"{base}..{ref}", timeout=60)


def working_diff(path: Path | str) -> str:
    """Uncommitted changes, including untracked files (via intent-to-add on a temp index)."""
    return git(path, "diff", "--no-color", "HEAD", check=False)


def log(path: Path | str, n: int = 30, ref: str = "HEAD") -> list[dict]:
    if not has_commits(path):
        return []
    fmt = "%H%x1f%h%x1f%an%x1f%at%x1f%s"
    out = git(path, "log", f"-n{n}", f"--format={fmt}", ref, check=False)
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            rows.append({"sha": parts[0], "short": parts[1], "author": parts[2],
                         "time": int(parts[3]), "subject": parts[4]})
    return rows


def merge_branch(repo: Path, branch: str, message: str) -> str:
    """Merge an agent branch into the user's checked-out branch. Refuses on a dirty tree."""
    dirty = dirty_files(repo)
    if dirty:
        raise GitError(f"refusing to merge: {len(dirty)} uncommitted change(s) in the project working tree")
    git(repo, "merge", "--no-ff", "-m", message, branch, identity=True, timeout=120)
    return head_commit(repo)
