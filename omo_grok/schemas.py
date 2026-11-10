"""Role-specific structured result schemas and a small strict validator (no extra deps)."""
from __future__ import annotations

import json
import re

from .errors import ValidationError

SHA40 = r"^[0-9a-f]{40}$"
SHA64 = r"^[0-9a-f]{64}$"
MAX_RESULT_BYTES = 256_000

# type spec: "str", "list", "dict", "bool", "int", ("enum", [...]), ("re", pattern)
SCHEMAS: dict[str, dict] = {
    "explore": {"required": {"repo_map": "str", "files": "list", "tests": "list", "constraints": "list",
                             "questions": "list"}, "min_artifacts": 1},
    "research": {"required": {"sources": "list", "api_requirements": "list", "risks": "list"}, "min_artifacts": 1,
                 "allow_not_applicable": True, "optional": {"citations": "list"}},
    "analyze": {"required": {"consumed_artifacts": "list", "gap_matrix": "list", "risks": "list",
                             "missing_decisions": "list", "suggested_criteria": "list"}, "min_artifacts": 1},
    "plan": {"required": {"work_units": "list", "acceptance_criteria": "list", "test_procedure": "str",
                          "rollback": "str", "unknowns": "list"}, "min_artifacts": 1, "plan_artifact": True},
    "plan_review": {"required": {"verdict": ("enum", ["accept", "reject"]), "plan_hash": ("re", SHA64),
                                 "findings": "list"}, "min_artifacts": 0},
    "implement": {"required": {"commit_sha": ("re", SHA40), "touched_files": "list", "summary": "str",
                               "unit_evidence": "str", "deviations": "list", "risks": "list"}, "min_artifacts": 0},
    "code_review": {"required": {"verdict": ("enum", ["accept", "reject"]), "revision": ("re", SHA40),
                                 "findings": "list"}, "min_artifacts": 0},
    "qa": {"required": {"verdict": ("enum", ["accept", "reject"]), "revision": ("re", SHA40), "steps": "list",
                        "criteria_map": "list", "findings": "list"}, "min_artifacts": 1},
    "integrate": {"required": {"decision": ("enum", ["finalize", "repair"]), "summary": "str"}, "min_artifacts": 0},
}

_PY = {"str": str, "list": list, "dict": dict, "bool": bool, "int": int}


def output_schema(kind: str) -> dict:
    """JSON-schema rendering for TaskPackets."""
    s = SCHEMAS[kind]
    props = {}
    for k, t in s["required"].items():
        if isinstance(t, tuple) and t[0] == "enum":
            props[k] = {"enum": t[1]}
        elif isinstance(t, tuple) and t[0] == "re":
            props[k] = {"type": "string", "pattern": t[1]}
        else:
            props[k] = {"type": {"str": "string", "list": "array", "dict": "object", "bool": "boolean",
                                 "int": "integer"}[t]}
    for k, t in s.get("optional", {}).items():
        props[k] = {"type": "array", "description": "engine receipt citations: [{receipt_id, quote}]"}
    out = {"type": "object", "required": sorted(s["required"]), "properties": props,
           "x-min-artifacts": s["min_artifacts"],
           "x-blocked-alternative": {"status": "blocked", "reason": "string"}}
    if s.get("allow_not_applicable"):
        out["x-not-applicable-alternative"] = {"not_applicable": True, "reason": "string"}
    if s.get("plan_artifact"):
        out["x-requires-artifact-kind"] = "plan"
    return out


def classify(kind: str, result: dict) -> str:
    """Return 'blocked', 'not_applicable' or 'normal' after validating the result for this kind."""
    if not isinstance(result, dict):
        raise ValidationError("result must be a JSON object")
    if len(json.dumps(result)) > MAX_RESULT_BYTES:
        raise ValidationError("result too large")
    if result.get("status") == "blocked":
        if not isinstance(result.get("reason"), str) or not result["reason"].strip():
            raise ValidationError("blocked result requires a factual 'reason'")
        return "blocked"
    s = SCHEMAS.get(kind)
    if s is None:
        raise ValidationError(f"unknown task kind {kind}")
    if result.get("not_applicable") is True:
        if not s.get("allow_not_applicable"):
            raise ValidationError(f"{kind} cannot be marked not_applicable")
        if not isinstance(result.get("reason"), str) or not result["reason"].strip():
            raise ValidationError("not_applicable requires an explicit 'reason'")
        return "not_applicable"
    for k, t in s["required"].items():
        if k not in result:
            raise ValidationError(f"result missing required field {k!r} for {kind}")
        v = result[k]
        if isinstance(t, tuple):
            if t[0] == "enum" and v not in t[1]:
                raise ValidationError(f"{k} must be one of {t[1]}")
            if t[0] == "re" and not (isinstance(v, str) and re.match(t[1], v)):
                raise ValidationError(f"{k} has invalid format")
        elif not isinstance(v, _PY[t]) or (t == "str" and not v.strip()):
            raise ValidationError(f"{k} must be a non-empty {t}")
    for k, t in s.get("optional", {}).items():
        if k in result and not isinstance(result[k], _PY[t]):
            raise ValidationError(f"{k} must be a {t}")
    return "normal"
