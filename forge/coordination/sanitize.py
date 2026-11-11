"""Sanitization for everything that leaves Forge through the coordination layer.

Outbound text (GitHub comments, coordination API, MCP) must not contain secrets,
private local paths, credentials, raw environment variables, or mentions that
would notify unrelated people. Inbound text from GitHub is normalised before it
is stored (HTML comments and invisible characters removed, length capped).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),                 # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}"),              # Anthropic / OpenAI keys
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),                # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),                            # AWS access key id
    re.compile(r"fgt_[A-Za-z0-9_-]{20,}"),                      # Forge coordination tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|passw(?:or)?d|credential)[A-Za-z0-9_]*)"
               r"(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"),
]
_ENV_LINE = re.compile(r"(?m)^(?:export\s+)?[A-Z][A-Z0-9_]{2,}=.*$")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_LOCAL_PATH = re.compile(r"(?<![\w.])(?:/Users|/home|/private|/var/folders|/tmp|/opt|/Volumes)/[^\s'\"`)\]>,;]*")
_MENTION = re.compile(r"(?<![\w`])@([A-Za-z0-9][A-Za-z0-9-]{0,38})")
_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def redact_secrets(text: str) -> str:
    for p in _SECRET_PATTERNS:
        if p.groups >= 3:
            text = p.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = p.sub("[redacted]", text)
    return text


def sanitize(text: str | None, *, roots: list[str | Path] | None = None, max_len: int = 4000,
             keep_mentions: bool = False) -> str:
    """Make text safe to publish outside Forge."""
    if not text:
        return ""
    s = str(text)
    s = redact_secrets(s)
    s = _ENV_LINE.sub("[environment line redacted]", s)
    # Project-relative paths first (most informative), then everything else local.
    for root in sorted({str(Path(r)) for r in roots or [] if r}, key=len, reverse=True):
        s = s.replace(root.rstrip("/") + "/", "").replace(root, ".")
    home = os.path.expanduser("~")
    s = _LOCAL_PATH.sub(_path_placeholder, s.replace(home + "/.forge/worktrees/", "/<worktree>/"))
    s = s.replace(home, "~")
    s = _EMAIL.sub(lambda m: m.group(0) if m.group(0).endswith("users.noreply.github.com") else "[email]", s)
    if not keep_mentions:
        s = _MENTION.sub(lambda m: "@​" + m.group(1), s)  # no notifications for arbitrary users
    s = _IMAGE.sub("[image removed]", s)
    if len(s) > max_len:
        s = s[: max_len - 20].rstrip() + "\n…(truncated)"
    return s


def _path_placeholder(m: re.Match) -> str:
    p = m.group(0)
    name = p.rstrip("/").rsplit("/", 1)[-1]
    if "<worktree>" in p:
        return "<worktree>"
    return f"<local-path>/{name}" if "." in name else "<local-path>"


def clean_inbound(text: str | None, max_len: int = 8000) -> str:
    """Normalise text received from GitHub before it is stored or used."""
    if not text:
        return ""
    s = _HTML_COMMENT.sub("", str(text))
    s = _INVISIBLE.sub("", s)
    s = s.replace("\r\n", "\n")
    return s[:max_len]
