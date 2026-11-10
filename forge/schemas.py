"""Structured handoff schemas.

Agents exchange these JSON documents (stored as artifacts) instead of relying
on chat transcripts. Schemas follow strict-mode rules (every property required,
no additional properties) so they work with both Claude --json-schema and
Codex --output-schema.
"""

from __future__ import annotations


def _obj(props: dict, ) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

PLAN = _obj({
    "summary": _STR,
    "steps": _STRS,
    "files_to_change": _STRS,
    "acceptance_criteria": _STRS,
    "test_plan": _STR,
    "risks": _STRS,
    "subtasks": {"type": "array", "items": _obj({
        "title": _STR, "objective": _STR,
        "depends_on": {"type": "array", "items": {"type": "integer"}},
    })},
})

HANDOFF = _obj({
    "summary": _STR,
    "files_changed": _STRS,
    "tests_added": _STRS,
    "notes_for_reviewer": _STR,
    "open_issues": _STRS,
})

REVIEW = _obj({
    "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
    "summary": _STR,
    "issues": {"type": "array", "items": _obj({
        "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
        "file": _STR,
        "description": _STR,
        "suggested_fix": _STR,
    })},
})

REPORT = _obj({
    "summary": _STR,
    "findings": _STRS,
    "evidence": _STRS,
    "next_steps": _STRS,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})

PROPOSAL = _obj({
    "title": _STR,
    "summary": _STR,
    "rationale": _STR,
    "steps": _STRS,
    "evidence": _STRS,
    "risks": _STRS,
    "success_criteria": _STRS,
    "estimated_effort": _STR,
})

BY_NAME = {"plan": PLAN, "handoff": HANDOFF, "review": REVIEW, "report": REPORT, "proposal": PROPOSAL}
