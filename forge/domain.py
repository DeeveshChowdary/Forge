"""Core domain types: task states, the transition table, and configuration."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"


TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
ACTIVE = {TaskState.PLANNING, TaskState.EXECUTING, TaskState.TESTING, TaskState.REVIEWING}

S = TaskState
# Allowed transitions. Anything not listed is rejected by TaskService.transition.
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    S.CREATED: {S.PLANNING, S.READY, S.BLOCKED, S.CANCELLED, S.PAUSED},
    # PLANNING may loop to READY for the next phase; it is also an active phase.
    S.PLANNING: {S.READY, S.FAILED, S.BLOCKED, S.PAUSED, S.CANCELLED, S.AWAITING_APPROVAL, S.COMPLETED},
    S.READY: {S.PLANNING, S.EXECUTING, S.TESTING, S.REVIEWING, S.BLOCKED, S.PAUSED, S.CANCELLED,
              S.AWAITING_APPROVAL, S.COMPLETED, S.FAILED},
    S.EXECUTING: {S.READY, S.TESTING, S.REVIEWING, S.FAILED, S.BLOCKED, S.PAUSED, S.CANCELLED,
                  S.AWAITING_APPROVAL, S.COMPLETED},
    S.TESTING: {S.READY, S.REVIEWING, S.EXECUTING, S.FAILED, S.PAUSED, S.CANCELLED,
                S.AWAITING_APPROVAL, S.COMPLETED},
    S.REVIEWING: {S.READY, S.EXECUTING, S.COMPLETED, S.FAILED, S.PAUSED, S.CANCELLED,
                  S.AWAITING_APPROVAL},
    S.AWAITING_APPROVAL: {S.READY, S.COMPLETED, S.FAILED, S.CANCELLED, S.PAUSED},
    S.BLOCKED: {S.READY, S.CANCELLED, S.FAILED, S.PAUSED},
    S.PAUSED: {S.READY, S.CREATED, S.BLOCKED, S.AWAITING_APPROVAL, S.CANCELLED},
    S.FAILED: {S.READY},          # explicit user retry only
    S.COMPLETED: set(),
    S.CANCELLED: set(),
}


def can_transition(a: TaskState | str, b: TaskState | str) -> bool:
    return TaskState(b) in TRANSITIONS[TaskState(a)]


class InvalidTransition(Exception):
    pass


RoutingMode = Literal["balanced", "max_quality", "economy", "manual"]
ROUTING_MODES = ("balanced", "max_quality", "economy", "manual")


class Limits(BaseModel):
    """Hard execution limits. Applied per task unless noted."""

    max_runtime_s: int = 1800          # wall clock per agent run
    max_task_runtime_s: int = 7200     # wall clock for the whole task
    max_turns: int = 40                # agent turns per run (where the provider supports it)
    max_review_cycles: int = 2         # revision loops after the first review
    max_retries: int = 1               # retries of a failed/interrupted phase
    max_concurrency: int = 2           # simultaneously executing tasks (per project)
    max_known_api_usd: float = 0.0     # directly billed API spend; 0 = no paid API usage
    test_timeout_s: int = 600


class Permissions(BaseModel):
    mode: Literal["read_only", "worktree_write"] = "worktree_write"
    allow_network_tools: bool = False       # web search / fetch for agents
    allow_agent_shell: bool = True          # agent may run shell commands in its sandbox
    allow_paid_api: bool = False            # never switch to billed APIs without this
    auto_merge: bool = False                # merging into the user's branch always needs approval unless set
    require_approval_for: list[str] = Field(
        default_factory=lambda: ["merge", "destructive", "publish", "deploy", "spending", "credentials", "out_of_scope"]
    )


class ProjectConfig(BaseModel):
    permissions: Permissions = Field(default_factory=Permissions)
    limits: Limits = Field(default_factory=Limits)
    test_command: str = ""                 # e.g. "python -m pytest -q"; empty = no automated tests
    default_routing_mode: RoutingMode = "balanced"
    default_workflow: str = "plan_implement_test_review"
    share_memory: bool = False             # read other projects' shared memory
    preferred_providers: list[str] = Field(default_factory=list)
    base_branch: str = ""                  # empty = whatever HEAD is at task creation

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "ProjectConfig":
        return cls.model_validate(raw or {})


class GlobalSettings(BaseModel):
    routing_mode: RoutingMode = "balanced"
    max_global_concurrency: int = 3
    allow_unverified_models: bool = False
    default_limits: Limits = Field(default_factory=Limits)
    codex_path: str = ""      # override auto-detection
    claude_path: str = ""
    claude_models: list[str] = Field(default_factory=lambda: ["sonnet", "opus", "haiku"])
    # Paid-API adapters do not exist yet; this flag is the switch that must be flipped
    # by the user (and still requires per-project allow_paid_api) before one is used.
    paid_api_globally_allowed: bool = False
