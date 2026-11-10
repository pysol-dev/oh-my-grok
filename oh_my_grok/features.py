"""Per-feature rollout modes (spec/ADAPTIVE.md section 10).

Every adaptive behaviour is behind one of these switches and every switch defaults to ``off``. The effective set is
copied into the job at submit (``jobs.policies_json.features``), so editing configuration later cannot change what a
running job does."""
from __future__ import annotations

from .errors import ValidationError

OFF, SHADOW, ENFORCE, ON = "off", "shadow", "enforce", "on"

FEATURE_MODES: dict[str, tuple[str, ...]] = {
    "adaptive_roster": (OFF, SHADOW, ENFORCE),
    "boulder": (OFF, ON),
    "lsp": (OFF, SHADOW, ENFORCE),
    "git_discipline": (OFF, SHADOW, ENFORCE),
    "docs_evidence": (OFF, SHADOW, ENFORCE),
    "repo_lease": (OFF, SHADOW, ENFORCE),
}


def defaults() -> dict[str, str]:
    return {name: OFF for name in FEATURE_MODES}


def normalize(raw) -> dict[str, str]:
    """Validate a (possibly partial) feature mapping and return the complete effective mapping."""
    if raw is None:
        return defaults()
    if not isinstance(raw, dict):
        raise ValidationError("features must be an object mapping feature name to mode")
    out = defaults()
    for name, mode in raw.items():
        if name not in FEATURE_MODES:
            raise ValidationError(f"unknown feature {name!r}", known=sorted(FEATURE_MODES))
        if mode not in FEATURE_MODES[name]:
            raise ValidationError(f"feature {name!r} mode must be one of {list(FEATURE_MODES[name])}, got {mode!r}")
        out[name] = mode
    return out


def frozen(policies: dict | None) -> dict[str, str]:
    """The features a job was submitted with. A job without a frozen set predates the feature and runs with all off."""
    return normalize((policies or {}).get("features"))
