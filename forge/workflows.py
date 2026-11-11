"""Workflow templates.

A workflow is an ordered list of phases. Each phase maps to a task state, an
agent role and a structured output schema. Review phases may loop back to a
`fix` phase, bounded by the task's `max_review_cycles` limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import TaskState


@dataclass(frozen=True)
class Phase:
    key: str
    state: TaskState
    role: str
    kind: str = "agent"         # agent | test | fanout | compare
    schema: str | None = None   # key into schemas.BY_NAME
    writes: bool = False        # needs a writable worktree
    instruction: str = ""
    max_turns: int | None = None        # per-phase default; task budget.phase_limits overrides
    max_runtime_s: int | None = None
    report: bool = False        # Forge writes the structured output to the task's report folder and commits it
    pairing: str | None = None  # "cross": each fan-out agent reviews another provider's earlier output


@dataclass(frozen=True)
class Workflow:
    key: str
    name: str
    description: str
    phases: tuple[Phase, ...]
    review_phase: str | None = None     # phase whose verdict may trigger a fix loop
    fix_phase: Phase | None = None
    category: str = "feature"
    read_only_ok: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    needs_worktree: bool = False        # Forge writes reports even though agents never write
    default_collaboration: str = "auto"

    def phase(self, key: str) -> Phase:
        for p in self.phases:
            if p.key == key:
                return p
        if self.fix_phase and self.fix_phase.key == key:
            return self.fix_phase
        raise KeyError(key)

    def index(self, key: str) -> int:
        return [p.key for p in self.phases].index(key)


S = TaskState

FIX = Phase("fix", S.EXECUTING, "fixer", schema="handoff", writes=True,
            instruction="Address the confirmed review issues and/or failing tests listed in the handoff. "
                        "Only change what is needed. Do not rewrite unrelated code.")

WORKFLOWS: dict[str, Workflow] = {w.key: w for w in [
    Workflow(
        "plan_implement_test_review", "Plan → Implement → Test → Review",
        "Standard feature workflow with an independent review and bounded fix loop.",
        (
            Phase("plan", S.PLANNING, "planner", schema="plan",
                  instruction="Inspect the repository and produce a concrete implementation plan. Do not modify files."),
            Phase("implement", S.EXECUTING, "implementer", schema="handoff", writes=True,
                  instruction="Implement the plan in this working tree. Add or update tests. Keep the change focused."),
            Phase("test", S.TESTING, "tester", kind="test"),
            Phase("review", S.REVIEWING, "reviewer", schema="review",
                  instruction="Independently review the diff against the objective and acceptance criteria. "
                              "Only report issues you can confirm from the code. Do not modify files."),
        ),
        review_phase="review", fix_phase=FIX, category="feature", tags=("code",),
    ),
    Workflow(
        "debug_fix", "Debug → Reproduce → Fix → Regression Test",
        "Reproduce a bug (ideally as a failing test), fix it, verify with a regression test, review.",
        (
            Phase("reproduce", S.PLANNING, "investigator", schema="report", writes=True,
                  instruction="Reproduce the bug. Prefer adding a failing regression test. Do not fix it yet."),
            Phase("fix_bug", S.EXECUTING, "implementer", schema="handoff", writes=True,
                  instruction="Fix the root cause so the regression test passes. Keep the change minimal."),
            Phase("test", S.TESTING, "tester", kind="test"),
            Phase("review", S.REVIEWING, "reviewer", schema="review",
                  instruction="Review the fix and the regression test. Confirm the root cause is addressed."),
        ),
        review_phase="review", fix_phase=FIX, category="bugfix", tags=("code",),
    ),
    Workflow(
        "research", "Research → Hypothesis → Experiment → Analyze",
        "Literature/code research, an explicit hypothesis, a small experiment in a worktree, and analysis.",
        (
            Phase("research", S.PLANNING, "researcher", schema="report",
                  instruction="Survey the relevant material in the repository. Cite file paths as evidence. "
                              "Separate facts found in sources from your interpretation."),
            Phase("hypothesis", S.PLANNING, "researcher", schema="report",
                  instruction="State one testable hypothesis, how it could be falsified, and the minimal experiment."),
            Phase("experiment", S.EXECUTING, "experimenter", schema="handoff", writes=True,
                  instruction="Implement the minimal experiment in this working tree and record raw results in files."),
            Phase("analyze", S.REVIEWING, "analyst", schema="report",
                  instruction="Analyse the experiment's outputs. State what is and is not supported by evidence."),
        ),
        category="research", tags=("research",),
    ),
    Workflow(
        "literature_review", "Literature Review → Independent Analysis → Novelty Assessment → Cross-Review → Decision",
        "Source-grounded literature research. No executable experiment. Claude and Codex investigate "
        "independently, cross-review each other against primary sources, and the workflow ends in a justified "
        "GO / PIVOT / REJECT / INCONCLUSIVE decision. Agents only read; Forge writes every report into the "
        "task's isolated worktree and commits it as a checkpoint.",
        (
            Phase("literature_review", S.PLANNING, "librarian", schema="research_report", report=True,
                  max_turns=80, max_runtime_s=1800,
                  instruction="Read the existing research files named in the objective (docs, reviews, matrices, "
                              "annotations, notes, proposals). Build a source-grounded inventory of what is known: "
                              "every claim must cite a repository-relative file path and a locator (section, "
                              "heading, page or line range). Mark each claim as verified_in_source, "
                              "source_reported, interpretation or unknown. Do not invent citations. Do not run "
                              "experiments, install anything, or check tools/environments."),
            Phase("independent_analysis", S.EXECUTING, "investigator", kind="fanout", schema="research_report",
                  report=True, max_turns=100, max_runtime_s=2400,
                  instruction="Investigate the research question independently, using the literature inventory "
                              "and the primary files it cites. Another model is investigating the same question "
                              "in parallel; do not try to coordinate. Every claim needs a source path and locator "
                              "and an honest evidence_type. Say 'unknown' instead of guessing."),
            Phase("novelty_assessment", S.REVIEWING, "assessor", schema="novelty", report=True,
                  max_turns=80, max_runtime_s=1800,
                  instruction="Using both independent analyses and the primary sources they cite, assess each "
                              "candidate contribution against the closest prior art. A finite seed review cannot "
                              "establish novelty: use 'unknown' when coverage is insufficient."),
            Phase("cross_review", S.REVIEWING, "cross_reviewer", kind="fanout", schema="cross_review",
                  report=True, pairing="cross", max_turns=80, max_runtime_s=1800,
                  instruction="Review the OTHER model's independent analysis (in the handoff). For every "
                              "disagreement with your own analysis, open the primary source and record what it "
                              "actually says. List claims that the cited sources do not support."),
            Phase("decision", S.REVIEWING, "decider", schema="decision", report=True,
                  max_turns=60, max_runtime_s=1500,
                  instruction="Decide GO, PIVOT, REJECT or INCONCLUSIVE for the proposed research direction. "
                              "Justify the decision only with cited evidence from the reports and primary "
                              "sources; record how disagreements were resolved and what remains open."),
        ),
        category="research", tags=("research", "literature"), needs_worktree=True,
        default_collaboration="independent_research",
    ),
    Workflow(
        "design_build_validate", "Design → Build → Validate → Deliver",
        "Design first, build, validate with tests and review, then deliver via an approved merge.",
        (
            Phase("design", S.PLANNING, "designer", schema="plan",
                  instruction="Produce a design: components, interfaces, data flow, and a validation plan."),
            Phase("build", S.EXECUTING, "builder", schema="handoff", writes=True,
                  instruction="Build the design in this working tree, including tests."),
            Phase("validate", S.TESTING, "tester", kind="test"),
            Phase("review", S.REVIEWING, "reviewer", schema="review",
                  instruction="Validate the build against the design and acceptance criteria."),
        ),
        review_phase="review", fix_phase=FIX, category="feature", tags=("code",),
    ),
    Workflow(
        "independent_proposals", "Independent Proposals → Compare",
        "Read-only: several agents independently propose a plan/milestone; Forge compares them.",
        (
            Phase("propose", S.PLANNING, "proposer", kind="fanout", schema="proposal",
                  instruction="Read the project and independently propose the next milestone. Ground every claim "
                              "in files you read (cite paths). Do not modify any files."),
            Phase("compare", S.REVIEWING, "comparator", kind="compare"),
        ),
        category="research", read_only_ok=True, tags=("read_only",),
    ),
    Workflow(
        "analyze", "Analyze (read-only)",
        "A single read-only agent investigates and reports. No files are modified.",
        (
            Phase("analyze", S.EXECUTING, "analyst", schema="report",
                  instruction="Investigate and report. Cite file paths as evidence. Do not modify files."),
        ),
        category="analysis", read_only_ok=True, tags=("read_only",),
    ),
]}


def get(key: str) -> Workflow:
    if key not in WORKFLOWS:
        raise KeyError(f"unknown workflow {key!r}; available: {sorted(WORKFLOWS)}")
    return WORKFLOWS[key]


def describe() -> list[dict]:
    return [{"key": w.key, "name": w.name, "description": w.description, "category": w.category,
             "read_only_ok": w.read_only_ok, "has_review_loop": bool(w.fix_phase),
             "default_collaboration": w.default_collaboration, "tags": list(w.tags),
             "phases": [{"key": p.key, "state": p.state.value, "role": p.role, "kind": p.kind, "writes": p.writes,
                         "max_turns": p.max_turns, "max_runtime_s": p.max_runtime_s, "report": p.report}
                        for p in w.phases]} for w in WORKFLOWS.values()]
