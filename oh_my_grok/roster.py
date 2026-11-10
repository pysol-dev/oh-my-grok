"""Intent, scope attestation and the smallest-adequate-team rules (spec/ADAPTIVE.md sections 4 and 5).

Pure functions only: nothing here touches the database, so the rules are testable in isolation and the engine is the
sole place that applies them."""
from __future__ import annotations

import re

from .errors import ValidationError

INTENTS = ("explanation", "investigation", "implementation")
CLASSES = ("quick", "standard", "heavy")
CLASS_RANK = {"none": 0, "quick": 1, "standard": 2, "heavy": 3}
INVESTIGATION_CLASS = "investigation"

SCOPE_KEYS = {"files_estimate": int, "new_dependency": bool, "external_api": bool, "security_sensitive": bool,
              "cross_cutting": bool}

# Task kinds a read-only intent can never own.
EDIT_SIDE_KINDS = frozenset({"implement", "code_review", "qa", "integrate"})
PLAN_SIDE_KINDS = frozenset({"plan", "plan_review"})
READ_ONLY_FORBIDDEN_KINDS = EDIT_SIDE_KINDS | PLAN_SIDE_KINDS


def validate_scope(scope) -> dict:
    """The attestation is complete on purpose: an omitted flag is not silently read as 'false'."""
    if not isinstance(scope, dict):
        raise ValidationError("scope must be an object with " + ", ".join(sorted(SCOPE_KEYS)))
    unknown = set(scope) - set(SCOPE_KEYS)
    if unknown:
        raise ValidationError("unknown scope keys", unknown=sorted(unknown), allowed=sorted(SCOPE_KEYS))
    missing = set(SCOPE_KEYS) - set(scope)
    if missing:
        raise ValidationError("scope attestation is incomplete", missing=sorted(missing))
    for key, typ in SCOPE_KEYS.items():
        v = scope[key]
        if typ is bool and not isinstance(v, bool):
            raise ValidationError(f"scope.{key} must be a boolean")
        if typ is int and (not isinstance(v, int) or isinstance(v, bool)):
            raise ValidationError(f"scope.{key} must be an integer")
    if not 1 <= scope["files_estimate"] <= 10000:
        raise ValidationError("scope.files_estimate must be 1..10000")
    return {k: scope[k] for k in sorted(SCOPE_KEYS)}


def floor(scope: dict) -> tuple[str, list[str]]:
    """Deterministic minimum class and the attested fields that forced it."""
    heavy = [k for k, hit in (("security_sensitive", scope["security_sensitive"]),
                              ("new_dependency", scope["new_dependency"]),
                              ("files_estimate>15", scope["files_estimate"] > 15)) if hit]
    if heavy:
        return "heavy", heavy
    standard = [k for k, hit in (("cross_cutting", scope["cross_cutting"]), ("external_api", scope["external_api"]),
                                 ("files_estimate>3", scope["files_estimate"] > 3)) if hit]
    if standard:
        return "standard", standard
    return "quick", []


def resolve_class(scope: dict, requested: str | None) -> str:
    """The class for a new implementation job: the floor, or a larger class. Never smaller."""
    fl, why = floor(scope)
    if requested is None:
        return fl
    if requested not in CLASSES:
        raise ValidationError(f"roster_class must be one of {list(CLASSES)}")
    if CLASS_RANK[requested] < CLASS_RANK[fl]:
        raise ValidationError(f"roster_class {requested!r} is below the floor {fl!r} required by the scope attestation",
                              floor=fl, triggered_by=why, requested=requested)
    return requested


def task_kinds(cls: str, scope: dict | None) -> list[str]:
    """Task kinds created at submit, in dependency order. Post-approval work is identical for every class."""
    if cls == "quick":
        return ["plan"]
    if cls == "standard":
        return ["explore", *(["research"] if scope and scope["external_api"] else []), "analyze", "plan"]
    if cls == "heavy":
        return ["explore", "research", "analyze", "plan"]
    if cls == INVESTIGATION_CLASS:
        research = scope is None or scope["external_api"]
        return ["explore", *(["research"] if research else []), "analyze"]
    raise ValidationError(f"no task set for class {cls!r}")


def research_kinds(cls: str, scope: dict | None) -> list[str]:
    return [k for k in task_kinds(cls, scope) if k in ("explore", "research")]


def check_escalation(current: str | None, target: str) -> None:
    if current not in CLASSES:
        raise ValidationError(f"job has class {current!r}; only quick/standard/heavy implementation jobs escalate")
    if target not in CLASSES:
        raise ValidationError(f"to_class must be one of {list(CLASSES)}")
    if CLASS_RANK[target] <= CLASS_RANK[current]:
        raise ValidationError(f"escalation only goes up: {current!r} -> {target!r} is refused",
                              current=current, target=target)


def next_class(current: str) -> str | None:
    i = CLASSES.index(current)
    return CLASSES[i + 1] if i + 1 < len(CLASSES) else None


_DEP_MANIFEST = re.compile(r"(^|/)(requirements[^/]*\.(txt|in)|[^/]*\.lock|pyproject\.toml|package\.json|"
                           r"package-lock\.json|Dockerfile)$")
_SECURITY_PATH = re.compile(r"(^\.github/|secret|credential|auth)", re.IGNORECASE)


def scope_contradictions(scope: dict, changed: list[str]) -> list[str]:
    """Facts in the real diff that contradict the attestation (spec section 5.4)."""
    out = []
    if len(changed) > 2 * scope["files_estimate"]:
        out.append(f"diff touches {len(changed)} files, attested {scope['files_estimate']} (limit is 2x)")
    if not scope["new_dependency"]:
        hits = sorted(p for p in changed if _DEP_MANIFEST.search(p))
        if hits:
            out.append("dependency manifest changed while new_dependency=false: " + ", ".join(hits[:5]))
    if not scope["security_sensitive"]:
        hits = sorted(p for p in changed if _SECURITY_PATH.search(p))
        if hits:
            out.append("security-sensitive path changed while security_sensitive=false: " + ", ".join(hits[:5]))
    return out
