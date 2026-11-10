"""Provider adapter interface.

Every model provider (CLI- or API-based) implements `ProviderAdapter`. The
engine and router only depend on this interface, so new providers can be added
by writing one module and registering it in `registry.py`.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

Billing = Literal["subscription", "api", "none", "unknown"]
AuthState = Literal["ok", "unverified", "expired", "missing", "unknown"]
ErrorKind = Literal["auth", "rate_limit", "timeout", "cancelled", "turn_limit", "model_unavailable",
                    "bad_output", "other"]


@dataclass
class ProviderStatus:
    provider: str
    display_name: str
    installed: bool
    path: str | None = None
    version: str | None = None
    auth_state: AuthState = "unknown"
    auth_detail: str = ""
    billing: Billing = "unknown"
    capabilities: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    is_mock: bool = False


@dataclass
class ModelInfo:
    model_id: str
    display_name: str
    source: Literal["discovered", "configured"]
    capabilities: dict[str, Any] = field(default_factory=dict)  # reasoning_levels, default_effort, ...
    hidden: bool = False


@dataclass
class ProbeResult:
    ok: bool
    resolved_id: str | None
    detail: str
    error_kind: ErrorKind | None = None


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    # "billed": real charge; "subscription_equivalent": provider-reported list-price estimate
    # for subscription usage (not charged); "unknown": provider did not report cost.
    cost_kind: Literal["billed", "subscription_equivalent", "unknown"] = "unknown"
    billing: Billing = "unknown"
    turns: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class ExecRequest:
    prompt: str
    cwd: Path
    role: str
    model: str | None = None
    effort: str | None = None
    sandbox: Literal["read_only", "workspace_write"] = "read_only"
    allow_shell: bool = False
    shell_allowlist: list[str] = field(default_factory=list)   # command prefixes, e.g. ["python", "pytest"]
    allow_network: bool = False
    output_schema: dict[str, Any] | None = None
    system_append: str = ""
    timeout_s: int = 1800
    max_turns: int | None = None
    resume_session_id: str | None = None
    log_path: Path | None = None
    extra_readable_dirs: list[Path] = field(default_factory=list)
    mcp_config_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)   # e.g. mock scenarios


@dataclass
class ExecResult:
    ok: bool
    text: str = ""
    structured: dict[str, Any] | None = None
    session_id: str | None = None
    resolved_model: str | None = None
    usage: Usage = field(default_factory=Usage)
    exit_code: int | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    duration_s: float = 0.0
    permission_denials: list[Any] = field(default_factory=list)


EventCallback = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]
"""on_event(type, message, data). Types: agent.message, agent.tool, agent.thinking, agent.raw, agent.error."""


class ProviderAdapter(abc.ABC):
    name: str
    display_name: str
    is_mock: bool = False

    @abc.abstractmethod
    def detect(self) -> ProviderStatus:
        """Installation, version, authentication state and capabilities. Must not modify auth."""

    @abc.abstractmethod
    def discover_models(self) -> list[ModelInfo]:
        """Models exposed by the provider, or configured entries when discovery is unavailable."""

    @abc.abstractmethod
    async def probe(self, model_id: str) -> ProbeResult:
        """A minimal, safe, read-only execution confirming the model actually works."""

    @abc.abstractmethod
    async def execute(self, req: ExecRequest, on_event: EventCallback, cancel: asyncio.Event,
                      on_start: Callable[[int], None] | None = None) -> ExecResult:
        """Run one agent session. Must honour req.timeout_s, req.max_turns and `cancel`."""

    def supported_efforts(self, model: ModelInfo | dict | None) -> list[str]:
        return []
