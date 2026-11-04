"""Deterministic verifier: runs a reviewed profile (argv list, shell=False) on an exact commit checkout."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .candidate_scan import scan_source
from . import oracles
from .config import (ORACLE_CHILD, RUNNER_NAME, TEMPLATE_MARKER, canonical_json, profile_assurance, profile_hash,
                     validate_profile)
from .paths import CODE_ROOT
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
    assurance: str = "none"  # oracle | cooperative | none (see docs/SECURITY.md)


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


SHADOW_SNAPSHOT = CODE_ROOT / "config" / "shadow_names.json"


def _snapshot_names() -> set[str]:
    """Install-time snapshot (config/shadow_names.json) so the list does not shrink with the host interpreter."""
    try:
        return set(json.loads(SHADOW_SNAPSHOT.read_text())["names"])
    except (OSError, ValueError, KeyError):
        return set()


def shadow_names(prof: dict) -> set[str]:
    return set(sys.stdlib_module_names) | VERIFIER_SENSITIVE | _site_names(prof["argv"][0]) | _snapshot_names() | \
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
    out += content_problems(repo, base, revision, prof, checkout)
    return out


DEFAULT_SOURCE_EXTENSIONS = [".py"]


def content_problems(repo: Path, base: str, revision: str, prof: dict, checkout: Path) -> list[str]:
    """Every added/modified file must be plain UTF-8 source with an allowed extension (no bytecode, binaries,
    __pycache__, symlinks or submodules), and every Python source must pass the static AST scan."""
    exts = tuple(prof.get("source_extensions", DEFAULT_SOURCE_EXTENSIONS))
    rules = prof.get("static_scan", {})
    out = []
    for status, mode, path in gitops.changed_entries(repo, base, revision):
        if status == "D":
            continue
        parts = path.split("/")
        if "__pycache__" in parts or path.endswith((".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib")):
            out.append(f"compiled/bytecode file in candidate: {path}")
            continue
        if mode == "120000":
            out.append(f"symlink in candidate: {path}")
            continue
        if mode == "160000":
            out.append(f"submodule in candidate: {path}")
            continue
        if not path.endswith(exts):
            out.append(f"non-source file (allowed extensions {list(exts)}): {path}")
            continue
        f = checkout / path
        try:
            data = f.read_bytes() if f.is_file() and not f.is_symlink() else None
        except OSError:
            data = None
        if data is None:
            out.append(f"cannot read candidate file {path} (fail closed)")
            continue
        if b"\0" in data:
            out.append(f"binary content in candidate source {path}")
            continue
        text, why = decode_source(data)
        if why:
            out.append(f"{why}: {path}")
            continue
        if path.endswith(".py"):
            found = scan_source(text, path, rules)
            if found:
                out.append("static scan rejected: " + "; ".join(found[:20]))
    return out


def decode_source(data: bytes) -> tuple[str | None, str | None]:
    """Decode exactly as the importer would (PEP 263 cookie / BOM via tokenize.detect_encoding) and accept only
    UTF-8: a `# -*- coding: unicode_escape -*-` (or any other) cookie would make the scanned text differ from what
    Python executes."""
    try:
        enc, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:
        return None, "invalid/mismatched encoding declaration or BOM"
    if enc.lower().replace("_", "-") not in ("utf-8", "utf8", "utf-8-sig"):
        return None, f"non-UTF-8 source encoding {enc!r} declared"
    try:
        return data.decode(enc), None
    except UnicodeDecodeError:
        return None, "non-UTF-8 candidate source"


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
        _purge_bytecode(dest)
        pyc = Path(tempfile.mkdtemp(prefix="omo-pyc-", dir=str(work_root)))
        try:
            if prof.get("runner") == RUNNER_NAME:
                return _run_python(prof, cwd, env, pyc, ph, eid)
            return _run_exit_code_only(prof, cwd, env, ph, eid)
        finally:
            shutil.rmtree(pyc, ignore_errors=True)
    finally:
        gitops.remove_temp_checkout(repo, dest)


def _timeout(e, ph, eid, argv):
    out = (e.stdout or b"")[:MAX_OUTPUT].decode("utf-8", "replace")
    err = (e.stderr or b"")[:MAX_OUTPUT].decode("utf-8", "replace")
    return VerifyOutcome("failed", None, True, redact(out), redact(err), "timeout (not success)", ph, eid, argv)


def _purge_bytecode(dest: Path) -> None:
    """The private checkout must never import stale/planted bytecode."""
    for p in list(dest.rglob("__pycache__")):
        if p.is_dir() and not p.is_symlink() and dest.resolve() in p.resolve().parents:
            shutil.rmtree(p)
    for p in list(dest.rglob("*.py[co]")):
        if p.is_file() or p.is_symlink():
            p.unlink()


def _run_exit_code_only(prof, cwd, env, ph, eid) -> VerifyOutcome:
    try:
        cp = subprocess.run(prof["argv"], cwd=str(cwd), env=env, shell=False, capture_output=True,
                            timeout=prof["timeout_seconds"], stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return _timeout(e, ph, eid, prof["argv"])
    out = cp.stdout[:MAX_OUTPUT].decode("utf-8", "replace")
    err = cp.stderr[:MAX_OUTPUT].decode("utf-8", "replace")
    status = "passed" if cp.returncode == 0 else "failed"
    return VerifyOutcome(status, cp.returncode, False, redact(out), redact(err),
                         f"assurance=cooperative: exit {cp.returncode} (exit-code-only profile; forgeable by the code "
                         f"under test; independent code_review and qa on this revision are required)",
                         ph, eid, prof["argv"], "cooperative")


def _py_argv(prof, pyc: Path, script) -> list[str]:
    """Interpreter + isolation flags from the profile, a throwaway pycache prefix, then the child script."""
    flags = [a for a in prof["argv"][1:-1] if a.startswith("-")]
    return [prof["argv"][0], *flags, "-X", f"pycache_prefix={pyc}", str(script)]


REPORT_PREFIX = "OMO-UNITTEST-REPORT "


def evaluate_report(rep: dict | None, returncode: int | None, prof: dict) -> list[str]:
    """Why the (untrusted, advisory) unittest child report is NOT clean. Empty list = the child CLAIMS a clean run."""
    why = []
    if returncode != 0:
        why.append(f"unittest child exit {returncode}")
    if not isinstance(rep, dict):
        why.append("no unittest report (process exited before reporting)")
        return why
    if rep.get("stage") != "done":
        why.append(f"unittest child stopped at stage {rep.get('stage')!r}")
    if rep.get("tamper"):
        why.append(f"test machinery tampered: {list(rep['tamper'])[:10]}")
    if rep.get("load_errors"):
        why.append(f"test load errors: {list(rep['load_errors'])[:5]}")
    expected = list(prof["expected_tests"])
    tests = rep.get("tests") if isinstance(rep.get("tests"), dict) else {}
    missing = [t for t in expected if t not in tests]
    if missing:
        why.append(f"expected tests not run: {missing}")
    notpass = {t: v for t, v in tests.items() if v != "passed"}
    if notpass:
        why.append(f"tests not passed: {notpass}")
    extra = sorted(set(tests) - set(expected))
    if extra:
        why.append(f"unexpected tests ran: {extra}")
    n = len(expected)
    want = {"run": n, "started": n, "failures": 0, "errors": 0, "skipped": 0, "expected_failures": 0,
            "unexpected_successes": 0}
    if rep.get("counts") != want:
        why.append(f"counts {rep.get('counts')} != expected {want}")
    return why


def _parse_report(stdout: str):
    for line in reversed(stdout.splitlines()):
        if line.startswith(REPORT_PREFIX):
            try:
                return json.loads(line[len(REPORT_PREFIX):])
            except ValueError:
                return None
    return None


def _run_unittest_child(prof, cwd, env, pyc):
    argv = [*_py_argv(prof, pyc, prof["argv"][-1]), prof["start_dir"]]
    cp = subprocess.run(argv, cwd=str(cwd), env=env, shell=False, capture_output=True,
                        timeout=prof["timeout_seconds"], stdin=subprocess.DEVNULL)
    out = cp.stdout[:MAX_OUTPUT].decode("utf-8", "replace")
    err = cp.stderr[:MAX_OUTPUT].decode("utf-8", "replace")
    return cp.returncode, out, err, evaluate_report(_parse_report(out), cp.returncode, prof)


MAX_LINE = 1_000_000


def run_oracle(prof, cwd, env, pyc) -> tuple[list[str], int, str]:
    """Trusted decision. The parent never imports candidate code; it draws fresh random inputs, sends them ONE AT A
    TIME (the next input does not exist until the previous answer arrives) to an untrusted child that calls the
    candidate, and recomputes every expected value itself. Returns (failure reasons, cases matched, child stderr)."""
    spec = prof["oracle"]
    cases = oracles.cases(spec["name"], spec.get("cases_per_function", 48))
    mods = sorted({c["call"].rsplit(".", 1)[0] for c in cases})
    deadline = time.monotonic() + prof["timeout_seconds"]
    with tempfile.TemporaryFile() as errf:
        p = subprocess.Popen([*_py_argv(prof, pyc, ORACLE_CHILD), *mods], cwd=str(cwd), env=env, shell=False,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1)
        lines: queue.Queue = queue.Queue()

        def pump():
            try:
                for ln in p.stdout:
                    lines.put(ln[:MAX_LINE + 1])
            finally:
                lines.put(None)
        threading.Thread(target=pump, daemon=True).start()

        def recv():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            ln = lines.get(timeout=left)
            if ln is None:
                raise EOFError
            if len(ln) > MAX_LINE:
                raise ValueError("oversized line")
            msg = json.loads(ln)
            if not isinstance(msg, dict):
                raise ValueError("non-object message")
            return msg

        why: list[str] = []
        matched = 0
        try:
            if recv() != {"ready": True}:
                why.append("oracle child did not report ready")
            for c in cases if not why else []:
                p.stdin.write(json.dumps({"id": c["id"], "call": c["call"], "args": c["args"]}) + "\n")
                p.stdin.flush()
                msg = recv()
                if set(msg) != {"id", "value"} or type(msg["id"]) is not int or msg["id"] != c["id"]:
                    why.append(f"oracle case {c['id']} ({c['call']}): bad/forged reply {sorted(msg)[:4]}")
                    break
                if not oracles.check_value(c, msg["value"]):
                    why.append(f"oracle case {c['id']} ({c['call']}): wrong value or type "
                               f"(got {type(msg['value']).__name__})")
                    break
                matched += 1
            if not why:
                p.stdin.write(json.dumps({"end": True}) + "\n")
                p.stdin.flush()
        except TimeoutError:
            why.append("oracle timeout (not success)")
        except queue.Empty:
            why.append("oracle timeout (not success)")
        except EOFError:
            why.append(f"oracle child exited after {matched}/{len(cases)} cases (exit/forgery cannot pass)")
        except (ValueError, KeyError, BrokenPipeError, OSError) as e:
            why.append(f"oracle protocol violation after {matched}/{len(cases)} cases: {type(e).__name__}")
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                rc = p.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
                rc = p.wait()
            errf.seek(0)
            err = errf.read(MAX_OUTPUT).decode("utf-8", "replace")
        if not why and matched != len(cases):
            why.append(f"only {matched}/{len(cases)} oracle cases matched")
        if not why and rc != 0:
            why.append(f"oracle child exit {rc}")
        return why, matched, err


def _run_python(prof, cwd, env, pyc, ph, eid) -> VerifyOutcome:
    assurance = profile_assurance(prof)
    try:
        rc, out, err, adv = _run_unittest_child(prof, cwd, env, pyc)
    except subprocess.TimeoutExpired as e:
        return _timeout(e, ph, eid, prof["argv"])
    out, err = redact(out), redact(err)
    if assurance == "oracle":
        why, matched, oerr = run_oracle(prof, cwd, env, pyc)
        err = err + ("\n--- oracle child stderr ---\n" + redact(oerr) if oerr else "")
        reasons = [f"ORACLE: {w}" for w in why] + [f"unittest (advisory, negative evidence still fails): {w}"
                                                    for w in adv]
        if reasons:
            return VerifyOutcome("failed", rc, False, out, err, "assurance=oracle: " + "; ".join(reasons), ph, eid,
                                 prof["argv"], assurance)
        return VerifyOutcome("passed", 0, False, out, err,
                             f"assurance=oracle: {matched}/{matched} fresh random oracle cases matched (parent-computed); "
                             f"unittest child (advisory) reports {len(prof['expected_tests'])} expected tests passed",
                             ph, eid, prof["argv"], assurance)
    if adv:
        return VerifyOutcome("failed", rc, False, out, err, "assurance=cooperative: " + "; ".join(adv), ph, eid,
                             prof["argv"], assurance)
    return VerifyOutcome("passed", 0, False, out, err,
                         f"assurance=cooperative: unittest child self-reports {len(prof['expected_tests'])} expected "
                         f"tests passed (forgeable by the code under test; independent code_review and qa on this "
                         f"revision are required)", ph, eid, prof["argv"], assurance)
