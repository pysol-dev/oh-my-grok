"""Non-secret configuration: policy defaults and verifier profiles (user-reviewed JSON)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import features
from .errors import ValidationError
from .paths import CODE_ROOT, node_path, python_path, site_packages, tools_dir

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
    "inline_dispatch": True,
    "projection_lock_seconds": 10,
    "context7_url": "https://mcp.context7.com/mcp",
    "context7_timeout_seconds": 30,
    "docs_max_per_job": 20,
    "git_max_commits": 50,
    # Host watchdog (features.watchdog). Defaults keep the service inert until an operator turns it on.
    "watchdog_interval_seconds": 60,
    "stall_awaiting_resume_seconds": 600,
    "watchdog_wake_cap": 3,
    "watchdog_stale_intervals": 3,
    "watchdog_log_max_bytes": 1_048_576,
    "watchdog_log_backups": 3,
    "features": {},
}

REMOVED_POLICY_KEYS = {"require_independent_assurance", "fixture_allow_reduced_assurance"}
TEMPLATE_MARKERS = ("OMG_VERIFY_TEMPLATE", "OMO_VERIFY_TEMPLATE")
TEMPLATE_MARKER = TEMPLATE_MARKERS[0]
RUNNER_NAME = "omg-unittest-child"
ACCEPTED_RUNNERS = frozenset({RUNNER_NAME, "omo-unittest-child"})


def template_marker_in(text: str) -> str | None:
    """Return the first fail-closed template marker found, or None. Both spellings count."""
    for marker in TEMPLATE_MARKERS:
        if marker in text:
            return marker
    return None


UNITTEST_CHILD = Path(__file__).resolve().parent / "unittest_child.py"   # untrusted child: advisory self-report
ORACLE_CHILD = Path(__file__).resolve().parent / "oracle_child.py"       # untrusted child: raw values only


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
        removed = set(data) & REMOVED_POLICY_KEYS
        if removed:
            raise ValidationError(f"policy keys {sorted(removed)} were removed: independent code_review + qa is "
                                  f"always required; reduced assurance is accepted per job, only through an approval "
                                  f"with accept_reduced_assurance=true")
        unknown = set(data) - set(DEFAULT_POLICY)
        if unknown:
            raise ValidationError(f"unknown policy keys: {sorted(unknown)}")
        pol.update(data)
    pol["features"] = features.normalize(pol.get("features"))
    return pol


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def placeholder_values() -> dict[str, str]:
    """Shipped config uses ${OMG_*}. The legacy ${OMO_*} spellings expand to the same values."""
    current = {"CODE_ROOT": str(CODE_ROOT), "PYTHON": python_path(), "TOOLS": str(tools_dir()),
               "NODE": node_path(), "SITE": site_packages()}
    out: dict[str, str] = {}
    for prefix in ("OMG_", "OMO_"):
        for suffix, value in current.items():
            out[prefix + suffix] = value
    return out


def expand_placeholders(obj, values: dict[str, str] | None = None):
    values = values or placeholder_values()

    def sub(m):
        if m.group(1) not in values:
            raise ValidationError(f"unknown config placeholder ${{{m.group(1)}}}", known=sorted(values))
        return values[m.group(1)]

    if isinstance(obj, str):
        return _PLACEHOLDER.sub(sub, obj)
    if isinstance(obj, list):
        return [expand_placeholders(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: expand_placeholders(v, values) for k, v in obj.items()}
    return obj


def _rewrite_legacy_child(arg: str) -> str:
    """Point a pre-rename trusted-child argv entry at oh_my_grok.

    Only the code root's own ``omo_grok/unittest_child.py`` or ``omo_grok/oracle_child.py`` is rewritten.
    Any other path is left alone so a profile cannot be silently retargeted.
    """
    if not isinstance(arg, str) or not arg.startswith("/"):
        return arg
    path = Path(arg)
    for name, current in (("unittest_child.py", UNITTEST_CHILD), ("oracle_child.py", ORACLE_CHILD)):
        if path == CODE_ROOT / "omo_grok" / name:
            return str(current)
    return arg


def _normalize_legacy_child_paths(prof: dict) -> dict:
    argv = prof.get("argv")
    if not isinstance(argv, list):
        return prof
    rewritten = [_rewrite_legacy_child(a) if isinstance(a, str) else a for a in argv]
    if rewritten == list(argv):
        return prof
    out = dict(prof)
    out["argv"] = rewritten
    return out


def load_verifier_profiles(config_dir: Path) -> dict:
    p = config_dir / "verifiers.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    profiles = expand_placeholders(data.get("profiles", {}))
    if isinstance(profiles, dict):
        profiles = {name: _normalize_legacy_child_paths(prof) if isinstance(prof, dict) else prof
                    for name, prof in profiles.items()}
    return profiles


REQUIRED_PROFILE_KEYS = {"argv": list, "cwd": str, "env_allow": list, "timeout_seconds": (int, float)}


def _oracle_problems(name: str, prof: dict) -> list[str]:
    from .oracles import ORACLES
    o = prof.get("oracle")
    if o is None:
        if name.startswith("fixture-"):
            return ["python fixture profiles must use an oracle (spec section 9: a fixture verifier that cannot be "
                    "forged by a self-report)"]
        return []
    out = []
    if not isinstance(o, dict) or o.get("name") not in ORACLES:
        return [f"oracle must be {{'name': one of {sorted(ORACLES)}, ...}}"]
    n = o.get("cases_per_function", 48)
    if not isinstance(n, int) or isinstance(n, bool) or not 8 <= n <= 2000:
        out.append("oracle.cases_per_function must be an int in 8..2000")
    mods = sorted({fn.call.rsplit(".", 1)[0] for fn in ORACLES[o["name"]]})
    if o.get("modules", mods) != mods:
        out.append(f"oracle.modules must be {mods} for oracle {o['name']!r}")
    return out


def _pure_problems(name: str, prof: dict) -> list[str]:
    """Fixture profiles must declare `pure_modules` (the pure-function rule for editable code, user decision H1)."""
    from .candidate_scan import PURE_IMPORT_CEILING
    pm = prof.get("pure_modules")
    if pm is None:
        return ["fixture profiles must declare pure_modules: {'allow_imports': [...]} (pure-function rule; H1 is not "
                "an accepted residual)"] if name.startswith("fixture-") else []
    if not isinstance(pm, dict) or set(pm) - {"allow_imports"} or not isinstance(pm.get("allow_imports", []), list):
        return ["pure_modules must be {'allow_imports': [module, ...]}"]
    extra = set(pm.get("allow_imports", [])) - PURE_IMPORT_CEILING
    if extra:
        return [f"pure_modules.allow_imports may only name pure modules {sorted(PURE_IMPORT_CEILING)}; "
                f"got {sorted(extra)}"]
    return []


def profile_assurance(prof: dict) -> str:
    """'oracle': the parent recomputed every value from fresh random inputs (decision does not trust the child).
    'cooperative': the pass rests on a self-report/exit status from a process that ran candidate code; adversarial
    candidate code can forge it, so independent code_review + qa on the same revision are the backstop."""
    return "oracle" if prof.get("oracle") else "cooperative"


def validate_profile(name: str, prof: dict) -> list[str]:
    """Return a list of problems; empty means structurally valid. Template marker fails closed."""
    problems = []
    hit = template_marker_in(canonical_json(prof)) or template_marker_in(name)
    if hit:
        problems.append(f"profile contains template marker {hit}; not a working verifier")
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
    is_python = bool(argv) and isinstance(argv[0], str) and Path(argv[0]).name.startswith("python")
    if is_python:
        flags = argv[1:argv.index("-m")] if "-m" in argv else argv[1:]
        if "-I" not in flags:
            problems.append("python verifiers must run isolated: put -I before -m/script (no cwd/user-site/PYTHON* "
                            "on sys.path)")
    if prof.get("runner") is not None or is_python:
        # exit status alone is not trusted for Python: the unittest child must report per-test results
        if prof.get("runner") not in ACCEPTED_RUNNERS:
            problems.append(f"python verifiers must use runner {RUNNER_NAME!r} "
                            f"(legacy 'omo-unittest-child' is still accepted; exit code alone is not trusted)")
        elif not argv or argv[-1] != str(UNITTEST_CHILD) or "-m" in argv:
            problems.append(f"python profiles must end argv with {UNITTEST_CHILD} and must not use -m")
        tests = prof.get("expected_tests")
        if not isinstance(tests, list) or not tests or not all(isinstance(t, str) and t for t in tests):
            problems.append("expected_tests must be a non-empty list of test ids")
        elif len(set(tests)) != len(tests):
            problems.append("expected_tests contains duplicates")
        if not isinstance(prof.get("start_dir"), str) or ".." in Path(prof.get("start_dir", "")).parts:
            problems.append("start_dir must be a relative path")
        if not isinstance(prof.get("editable_paths"), list) or not prof.get("editable_paths"):
            problems.append("python profiles must declare editable_paths")
        if "type_checks" in prof:
            problems.append("type_checks (in-process self-report) were removed; use an oracle profile")
        problems += _oracle_problems(name, prof)
        problems += _pure_problems(name, prof)
    elif prof.get("exit_code_only") is not True:
        problems.append("non-python profiles must set exit_code_only: true to accept exit-status-only (reduced) "
                        "assurance")
    scan = prof.get("static_scan", {})
    if not isinstance(scan, dict) or "enabled" in scan:
        problems.append("static_scan must be a dict of allow/forbid lists; scanning cannot be disabled")
    for key in ("protected_paths", "editable_paths"):
        v = prof.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x and not x.startswith("/")
                                                                  and ".." not in Path(x).parts for x in v)):
            problems.append(f"{key} must be a list of relative paths without '..'")
    to = prof.get("timeout_seconds")
    if isinstance(to, (int, float)) and not (0 < to <= 3600):
        problems.append("timeout_seconds must be in (0, 3600]")
    return problems


def profile_hash(prof: dict) -> str:
    return sha256_text(canonical_json(prof))
