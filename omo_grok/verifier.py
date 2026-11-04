"""Deterministic verifier: runs a reviewed profile (argv list, shell=False) on an exact commit checkout."""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .config import TEMPLATE_MARKER, canonical_json, profile_hash, validate_profile
from .redact import redact

MAX_OUTPUT = 1_000_000


@dataclass
class VerifyOutcome:
    status: str  # passed | failed | error
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    detail: str
    profile_hash: str
    env_identity: str
    argv: list = field(default_factory=list)


def env_identity(prof: dict) -> str:
    exe = prof.get("argv", ["?"])[0]
    try:
        st = os.stat(exe)
        exe_id = f"{os.path.realpath(exe)}:{st.st_size}"
    except OSError:
        exe_id = f"{exe}:missing"
    return hashlib.sha256(canonical_json({"py": sys.version, "plat": platform.platform(), "exe": exe_id}).encode()).hexdigest()


# Names a candidate must never (re)define on an import root: the stdlib, test runners/import hooks, and
# everything installed in the verifier interpreter's site-packages.
VERIFIER_SENSITIVE = {"unittest", "pytest", "_pytest", "py", "pluggy", "conftest", "sitecustomize", "usercustomize",
                      "site", "pkg_resources", "setuptools", "_distutils_hack", "pip", "coverage", "nose", "doctest",
                      "encodings", "importlib", "runpy"}
HOOK_FILES = {"sitecustomize.py", "usercustomize.py", "conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
              "pyproject.toml", ".pth"}
MODULE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyw", ".so", ".pyd")


def _under(path: str, prefix: str) -> bool:
    p = prefix.rstrip("/")
    return p in ("", ".") or path == p or path.startswith(p + "/")


def _site_names(exe: str) -> set[str]:
    names: set[str] = set()
    root = Path(os.path.realpath(exe)).parent.parent / "lib"
    for sp in root.glob("python*/site-packages"):
        for e in sp.iterdir():
            n = e.name
            if n.endswith((".dist-info", ".egg-info")) or n.startswith("__"):
                continue
            names.add(n.split(".")[0])
    return names


def shadow_names(prof: dict) -> set[str]:
    return set(sys.stdlib_module_names) | VERIFIER_SENSITIVE | _site_names(prof["argv"][0]) | \
        set(prof.get("extra_shadow_names", []))


def import_roots(prof: dict, checkout: Path) -> set[str]:
    """Checkout-relative dirs that may end up on sys.path: cwd and any directory named in argv (e.g. -s/-t)."""
    cwd = os.path.normpath(prof.get("cwd", "."))
    roots = {"" if cwd == "." else cwd}
    for a in prof["argv"][1:]:
        if a.startswith("-") or a.startswith("/"):
            continue
        rel = os.path.normpath(os.path.join(cwd, a))
        if not rel.startswith("..") and (checkout / rel).is_dir():
            roots.add("" if rel == "." else rel)
    return roots


def scope_problems(repo: Path, revision: str, prof: dict, checkout: Path) -> list[str]:
    """Static, pre-execution rejection of candidates that change verifier-controlled files, edit outside the
    profile's editable allowlist, or add/modify anything that could shadow an imported module or hook start-up."""
    base = prof.get("protected_base")
    if not base:
        return ["no base revision to diff against; cannot check candidate scope (fail closed)"]
    changed = gitops.changed_files(repo, base, revision)
    out = []
    protected = prof.get("protected_paths", [])
    hit = [c for c in changed if any(_under(c, p) for p in protected)]
    if hit:
        out.append(f"protected verifier paths modified: {hit}")
    editable = prof.get("editable_paths")
    if editable is not None:
        outside = [c for c in changed if not any(_under(c, p) for p in editable)]
        if outside:
            out.append(f"paths outside the profile's editable_paths {editable} modified: {outside}")
    hooks = [c for c in changed if Path(c).name in HOOK_FILES or c.endswith(".pth")]
    if hooks:
        out.append(f"interpreter/test-runner hook files added or modified: {hooks}")
    names = shadow_names(prof)
    shadow = []
    for c in changed:
        for root in import_roots(prof, checkout):
            if not _under(c, root):
                continue
            rel = c[len(root) + 1:] if root else c
            top = rel.split("/")[0]
            if "/" in rel:  # a package directory; count it if it is (or was) a regular package or holds modules
                mod = top
            elif top.endswith(MODULE_SUFFIXES) or ".cpython-" in top:
                mod = top.split(".")[0]
            else:
                continue
            if mod in names:
                shadow.append(c)
    if shadow:
        out.append(f"candidate adds/modifies top-level modules shadowing stdlib/verifier modules: {sorted(set(shadow))}")
    return out


def preflight(name: str | None, prof: dict | None) -> list[str]:
    if not name or prof is None:
        return ["no verifier profile configured for this job (fail closed)"]
    problems = validate_profile(name, prof)
    if not problems:
        exe = prof["argv"][0]
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            problems.append(f"verifier executable not found or not executable: {exe}")
    return problems


def run(repo: Path, revision: str, name: str | None, prof: dict | None, work_root: Path) -> VerifyOutcome:
    ph = profile_hash(prof) if prof else "none"
    eid = env_identity(prof) if prof else "none"
    problems = preflight(name, prof)
    if problems:
        return VerifyOutcome("error", None, False, "", "", "; ".join(problems), ph, eid, (prof or {}).get("argv", []))
    if not gitops.commit_exists(repo, revision):
        return VerifyOutcome("error", None, False, "", "", f"revision {revision} not found", ph, eid, prof["argv"])
    dest = work_root / f"verify-{revision[:12]}-{uuid.uuid4().hex[:8]}"
    gitops.temp_checkout(repo, revision, dest)
    try:
        # template marker inside any verifier script referenced from the checkout fails closed
        for a in prof["argv"][1:]:
            cand = (dest / prof["cwd"] / a)
            if cand.is_file() and not cand.is_symlink():
                if TEMPLATE_MARKER.encode() in cand.read_bytes()[:200_000]:
                    return VerifyOutcome("error", None, False, "", "", f"{a} contains {TEMPLATE_MARKER}; fail closed",
                                         ph, eid, prof["argv"])
        scope = scope_problems(repo, revision, prof, dest)
        if scope:
            return VerifyOutcome("failed", None, False, "", "", "candidate rejected before running: " + "; ".join(scope),
                                 ph, eid, prof["argv"])
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(dest),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"}
        for k in prof.get("env_allow", []):
            if k in os.environ:
                env[k] = os.environ[k]
        env.update({k: str(v) for k, v in prof.get("env_set", {}).items()})
        cwd = (dest / prof["cwd"]).resolve()
        if dest.resolve() not in [cwd, *cwd.parents]:
            return VerifyOutcome("error", None, False, "", "", "cwd escapes checkout", ph, eid, prof["argv"])
        try:
            cp = subprocess.run(prof["argv"], cwd=str(cwd), env=env, shell=False, capture_output=True,
                                timeout=prof["timeout_seconds"], stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"")[:MAX_OUTPUT].decode("utf-8", "replace")
            err = (e.stderr or b"")[:MAX_OUTPUT].decode("utf-8", "replace")
            return VerifyOutcome("failed", None, True, redact(out), redact(err), "timeout (not success)", ph, eid,
                                 prof["argv"])
        out = cp.stdout[:MAX_OUTPUT].decode("utf-8", "replace")
        err = cp.stderr[:MAX_OUTPUT].decode("utf-8", "replace")
        status = "passed" if cp.returncode == 0 else "failed"
        return VerifyOutcome(status, cp.returncode, False, redact(out), redact(err),
                             f"exit {cp.returncode} (only the exit status decides; output text is ignored)",
                             ph, eid, prof["argv"])
    finally:
        gitops.remove_temp_checkout(repo, dest)
