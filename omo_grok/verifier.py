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
        base = prof.get("protected_base")
        protected = prof.get("protected_paths", [])
        if protected and base:
            changed = gitops.changed_files(repo, base, revision)
            hit = [c for c in changed if any(c == p.rstrip("/") or c.startswith(p.rstrip("/") + "/") for p in protected)]
            if hit:
                return VerifyOutcome("failed", None, False, "", "", f"protected verifier paths modified: {hit}",
                                     ph, eid, prof["argv"])
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(dest),
               "PYTHONDONTWRITEBYTECODE": "1"}
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
