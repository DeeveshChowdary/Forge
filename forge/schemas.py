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

_EVIDENCE = _obj({
    "claim": _STR,
    "source": {"type": "string", "description": "repository-relative file path (or 'none')"},
    "locator": {"type": "string", "description": "section, heading, page or line range in the source"},
    "evidence_type": {"type": "string", "enum": ["verified_in_source", "source_reported", "interpretation",
                                                 "unknown"]},
})

RESEARCH_REPORT = _obj({
    "summary": _STR,
    "claims": {"type": "array", "items": _EVIDENCE},
    "open_questions": _STRS,
    "limitations": _STRS,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})

NOVELTY = _obj({
    "summary": _STR,
    "candidates": {"type": "array", "items": _obj({
        "contribution": _STR,
        "closest_prior_art": _STRS,
        "overlap": _STR,
        "assessment": {"type": "string", "enum": ["likely_novel", "incremental", "already_covered", "unknown"]},
        "evidence": {"type": "array", "items": _EVIDENCE},
    })},
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})

CROSS_REVIEW = _obj({
    "summary": _STR,
    "agreements": _STRS,
    "disagreements": {"type": "array", "items": _obj({
        "topic": _STR, "this_position": _STR, "other_position": _STR,
        "primary_source_check": _STR,
        "resolution": {"type": "string", "enum": ["other_correct", "this_correct", "both_partially", "unresolved"]},
    })},
    "unsupported_claims_found": _STRS,
})

DECISION = _obj({
    "decision": {"type": "string", "enum": ["GO", "PIVOT", "REJECT", "INCONCLUSIVE"]},
    "justification": _STR,
    "evidence": {"type": "array", "items": _EVIDENCE},
    "disagreements_resolved": _STRS,
    "open_questions": _STRS,
    "next_steps": _STRS,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})

BY_NAME = {"plan": PLAN, "handoff": HANDOFF, "review": REVIEW, "report": REPORT, "proposal": PROPOSAL,
           "research_report": RESEARCH_REPORT, "novelty": NOVELTY, "cross_review": CROSS_REVIEW,
           "decision": DECISION}
