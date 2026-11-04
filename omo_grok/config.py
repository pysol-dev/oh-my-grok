"""Non-secret configuration: policy defaults and verifier profiles (user-reviewed JSON)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import ValidationError

DEFAULT_POLICY = {
    "max_repair_cycles": 3,
    "max_plan_rounds": 3,
    "max_attempts_per_task": 3,
    "lease_seconds": 1800,
    "rewake_after_seconds": 900,
    "max_rewakes": 3,
    "dispatch_max_retries": 5,
    "backoff_base_seconds": 30,
    "backoff_cap_seconds": 1800,
    "http_connect_timeout_seconds": 5,
    "http_read_timeout_seconds": 15,
    "per_bot_hourly_dispatch_limit": 20,
    "max_sends_per_flush": 10,
    "repo_allowlist": [],
    "require_independent_assurance": True,
    "fixture_allow_reduced_assurance": True,
    "inline_dispatch": True,
}

TEMPLATE_MARKER = "OMO_VERIFY_TEMPLATE"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def load_policy(config_dir: Path) -> dict:
    pol = dict(DEFAULT_POLICY)
    p = config_dir / "policy.json"
    if p.exists():
        data = json.loads(p.read_text())
        unknown = set(data) - set(DEFAULT_POLICY)
        if unknown:
            raise ValidationError(f"unknown policy keys: {sorted(unknown)}")
        pol.update(data)
    return pol


def load_verifier_profiles(config_dir: Path) -> dict:
    p = config_dir / "verifiers.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return data.get("profiles", {})


REQUIRED_PROFILE_KEYS = {"argv": list, "cwd": str, "env_allow": list, "timeout_seconds": (int, float)}


def validate_profile(name: str, prof: dict) -> list[str]:
    """Return a list of problems; empty means structurally valid. Template marker fails closed."""
    problems = []
    if TEMPLATE_MARKER in canonical_json(prof) or TEMPLATE_MARKER in name:
        problems.append(f"profile contains template marker {TEMPLATE_MARKER}; not a working verifier")
    for k, t in REQUIRED_PROFILE_KEYS.items():
        if k not in prof or not isinstance(prof[k], t):
            problems.append(f"profile key {k!r} missing or wrong type")
    argv = prof.get("argv") or []
    if not argv or not all(isinstance(a, str) and a for a in argv):
        problems.append("argv must be a non-empty list of strings")
    elif not argv[0].startswith("/"):
        problems.append("argv[0] must be an absolute, pinned executable path")
    cwd = prof.get("cwd", "")
    if isinstance(cwd, str) and (cwd.startswith("/") or ".." in Path(cwd).parts):
        problems.append("cwd must be relative to the checkout and must not contain '..'")
    to = prof.get("timeout_seconds")
    if isinstance(to, (int, float)) and not (0 < to <= 3600):
        problems.append("timeout_seconds must be in (0, 3600]")
    return problems


def profile_hash(prof: dict) -> str:
    return sha256_text(canonical_json(prof))
