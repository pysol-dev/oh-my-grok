"""Deterministic verifier: runs a reviewed profile (argv list, shell=False) on an exact commit checkout."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import platform
import queue
import random
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops, integrity, sandbox
from .candidate_scan import pure_module_problems, scan_source
from . import oracle_child, oracles
from .config import (RUNNER_NAME, UNITTEST_CHILD, TEMPLATE_MARKER, canonical_json, profile_assurance, profile_hash,
                     validate_profile)
from .paths import CODE_ROOT
from .redact import redact

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
    sandbox: str = "none"    # bwrap | none (none = candidate code was not run)
    tamper: list = field(default_factory=list)


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
    if prof.get("pure_modules") is not None:
        out += pure_tree_problems(prof, checkout)
    return out


def pure_tree_problems(prof: dict, checkout: Path) -> list[str]:
    """Pure-function rule over EVERY .py file under the editable paths of the checked-out revision (changed or not),
    so that a relative import can only reach modules that are themselves scanned."""
    allow = prof["pure_modules"].get("allow_imports", [])
    files: dict[str, Path] = {}
    for d, dirs, names in os.walk(checkout):
        dirs[:] = sorted(x for x in dirs if x != ".git")
        for n in sorted(names):
            f = Path(d) / n
            rel = f.relative_to(checkout).as_posix()
            if rel.endswith(".py") and any(_under(rel, e) for e in prof.get("editable_paths", [])):
                files[rel] = f
    out: list[str] = []
    local = frozenset(files)
    for rel, f in files.items():
        if f.is_symlink() or not f.is_file():
            out.append(f"pure-function rule rejected: {rel} is not a regular file")
            continue
        text, why = decode_source(f.read_bytes())
        if why:
            out.append(f"pure-function rule rejected: {why}: {rel}")
            continue
        found = pure_module_problems(text, rel, allow, local)
        if found:
            out.append("pure-function rule rejected: " + "; ".join(found[:20]))
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


@dataclass
class _Ctx:
    """One sandboxed verification run: everything a child needs, nothing the candidate may see."""
    sb: sandbox.Probe
    exe: str              # interpreter/command inside the read-only runtime
    work: Path            # per-run COPY of the checkout (writable scratch, bound at /work)
    harness: Path | None  # per-run read-only copy of the child scripts (bound at /omo-harness)
    cwd_rel: str
    env: dict
    timeout: float
    staging: Path | None = None  # private per-verification temp dir; every child run gets a FRESH copy of `work`

    def fresh(self, tag: str) -> "_Ctx":
        """A new copy of the pristine checkout for one child run (no state carries over between runs)."""
        if self.staging is None:
            return self
        dst = self.staging / f"{tag}-{uuid.uuid4().hex[:6]}"
        shutil.copytree(self.work, dst, symlinks=True)
        return dataclasses.replace(self, work=dst)


def _err(status, detail, ph, eid, prof, sb="none", **kw) -> VerifyOutcome:
    return VerifyOutcome(status, None, False, "", "", detail, ph, eid, (prof or {}).get("argv", []), sandbox=sb, **kw)


def run(repo: Path, revision: str, name: str | None, prof: dict | None, work_root: Path) -> VerifyOutcome:
    ph = profile_hash(prof) if prof else "none"
    eid = env_identity(prof) if prof else "none"
    problems = preflight(name, prof)
    if problems:
        return _err("error", "; ".join(problems), ph, eid, prof)
    sb = sandbox.probe()
    if not sb.ok:  # never run candidate code unsandboxed
        return _err("error", f"{sb.detail}; refusing to run candidate code without a sandbox (fail closed)", ph, eid,
                    prof)
    exe = sandbox.runtime_executable(prof["argv"][0])
    if not exe:
        return _err("error", f"verifier executable {prof['argv'][0]} does not resolve inside the read-only sandbox "
                    f"runtime {sandbox.RUNTIME_ROOT} (fail closed)", ph, eid, prof)
    cwd_rel = os.path.normpath(prof["cwd"])
    if cwd_rel.startswith("..") or os.path.isabs(cwd_rel):
        return _err("error", "cwd escapes checkout", ph, eid, prof)
    if not gitops.commit_exists(repo, revision):
        return _err("error", f"revision {revision} not found", ph, eid, prof)
    work_root.mkdir(parents=True, exist_ok=True)
    dest = work_root / f"verify-{revision[:12]}-{uuid.uuid4().hex[:8]}"
    gitops.temp_checkout(repo, revision, dest)
    try:
        # template marker inside any verifier script referenced from the checkout fails closed
        for a in prof["argv"][1:]:
            cand = (dest / prof["cwd"] / a)
            if cand.is_file() and not cand.is_symlink():
                if TEMPLATE_MARKER.encode() in cand.read_bytes()[:200_000]:
                    return _err("error", f"{a} contains {TEMPLATE_MARKER}; fail closed", ph, eid, prof)
        scope = scope_problems(repo, revision, prof, dest)
        if scope:
            return _err("failed", "candidate rejected before running: " + "; ".join(scope), ph, eid, prof)
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": sandbox.SANDBOX_WORK,
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"}
        for k in prof.get("env_allow", []):
            if k in os.environ:
                env[k] = os.environ[k]
        env.update({k: str(v) for k, v in prof.get("env_set", {}).items()})
        python = prof.get("runner") == RUNNER_NAME
        before = integrity.snapshot()
        # staged under the system temp dir with a neutral name: host paths are visible inside the sandbox via
        # /proc/1/cmdline and /proc/self/mountinfo, so they must not carry harness/job markers
        staging = Path(tempfile.mkdtemp())
        try:
            work, harness = sandbox.stage(dest, [UNITTEST_CHILD] if python else [], staging)
            _purge_bytecode(work)
            if not (work / cwd_rel).is_dir():
                return _err("error", "profile cwd does not exist in the checkout", ph, eid, prof, sb.label)
            ctx = _Ctx(sb, exe, work, harness if python else None, cwd_rel, env, float(prof["timeout_seconds"]),
                       staging)
            out = _run_python(prof, ctx, ph, eid) if python else _run_exit_code_only(prof, ctx, ph, eid)
        finally:
            left = _rmtree(staging)
        changes = integrity.diff(before, integrity.snapshot())
        out.sandbox = sb.label
        if left:  # never claim a clean cleanup that did not happen (does not affect pass/fail)
            out.stderr = (out.stderr or "") + f"\n[verifier] scratch cleanup incomplete: {left} entries left in {staging}"
        if changes:
            out.status, out.tamper = "failed", changes
            out.detail = (f"TAMPER: trusted files changed during verification ({len(changes)}): "
                          f"{'; '.join(changes[:5])} | was: {out.detail}")
        return out
    finally:
        gitops.remove_temp_checkout(repo, dest)


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
RMTREE_MAX_ENTRIES = 1_000_000  # sane bound on one staging tree; beyond it the rest is left (and reported)


def _chmod_dir_nofollow(name: str, dir_fd: int, expect: os.stat_result) -> bool:
    """Make one listed directory owner-rwx without ever following a symlink: fchmodat(dir_fd, name,
    AT_SYMLINK_NOFOLLOW) after lstat (relative to the same dir fd) confirms it is still the same directory inode."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino):
            return False
        if st.st_mode & 0o700 != 0o700:
            os.chmod(name, 0o700, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except (OSError, NotImplementedError):
        return False


def _rmtree(p: Path) -> int:
    """Delete a per-run staging tree that sandboxed code could write to (all sandboxed processes are dead by now).
    Returns the number of entries that could NOT be removed (0 = clean).

    Never follows a symlink and never chmods by an absolute path: every entry is classified with lstat relative to
    its parent directory fd; symlinks and files are unlinked; a directory is first made owner-rwx with
    fchmodat(AT_SYMLINK_NOFOLLOW) after re-checking its inode (so 000 / 0o100 modes do not block cleanup), then
    MOVED (rename relative to dir fds, which never follows symlinks) to the top of the tree under a fresh name and
    processed from there. The walk is therefore iterative and flat: arbitrarily deep trees need O(1) open fds and no
    recursion. Bounded by RMTREE_MAX_ENTRIES."""
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(p)
            return 0
        except OSError:
            return 1
    parent_fd = os.open(p.parent, _DIR_FLAGS)
    try:
        if not _chmod_dir_nofollow(p.name, parent_fd, st):
            return 1
        try:
            root = os.open(p.name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError:
            return 1
        try:
            fst = os.fstat(root)
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                return 1
            left = _drain_flat(root)
        finally:
            os.close(root)
        try:
            os.rmdir(p.name, dir_fd=parent_fd)
        except OSError:
            left += 1
        return left
    finally:
        os.close(parent_fd)


def _drain_flat(root: int) -> int:
    pending = [None]  # None = the root itself; strings = directories moved to the root
    seen = 0
    counter = 0
    left = 0
    while pending:
        name = pending.pop()
        try:
            fd = root if name is None else os.open(name, _DIR_FLAGS, dir_fd=root)
        except OSError:
            left += 1
            continue
        try:
            if name is not None:
                fst = os.fstat(fd)
                lst = os.stat(name, dir_fd=root, follow_symlinks=False)
                if (fst.st_dev, fst.st_ino) != (lst.st_dev, lst.st_ino):
                    left += 1
                    continue
            try:
                entries = list(os.scandir(fd))
            except OSError:
                left += 1
                continue
            for e in entries:
                seen += 1
                if seen > RMTREE_MAX_ENTRIES:
                    return left + 1
                try:
                    est = e.stat(follow_symlinks=False)
                except OSError:
                    left += 1
                    continue
                if stat.S_ISDIR(est.st_mode):
                    if not _chmod_dir_nofollow(e.name, fd, est):
                        left += 1
                        continue
                    counter += 1
                    flat = f".rm{counter}-{uuid.uuid4().hex[:8]}"
                    try:
                        os.rename(e.name, flat, src_dir_fd=fd, dst_dir_fd=root)
                    except OSError:
                        left += 1
                        continue
                    pending.append(flat)
                else:
                    try:
                        os.unlink(e.name, dir_fd=fd)
                    except OSError:
                        left += 1
        finally:
            if fd != root:
                os.close(fd)
        if name is not None:
            # its subdirectories were moved out and its files unlinked: it is empty now
            try:
                os.rmdir(name, dir_fd=root)
            except OSError:
                left += 1
    return left


def _purge_bytecode(dest: Path) -> None:
    """The private checkout copy must never import stale/planted bytecode."""
    for p in list(dest.rglob("__pycache__")):
        if p.is_dir() and not p.is_symlink() and dest.resolve() in p.resolve().parents:
            shutil.rmtree(p)
    for p in list(dest.rglob("*.py[co]")):
        if p.is_file() or p.is_symlink():
            p.unlink()


MAX_OUTPUT = 1_000_000      # bytes kept per stream; the rest is drained and discarded
MAX_LINE = 1_000_000        # one oracle protocol line
MAX_PROTOCOL = 16_000_000   # total oracle protocol bytes


def _drain(stream, keep: list, limit: int) -> None:
    n = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            if n < limit:
                keep.append(chunk[:limit - n])
            n += len(chunk)
    except (OSError, ValueError):
        return


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, 9)
    except OSError:
        pass
    try:
        p.kill()
    except OSError:
        pass


def _spawn(ctx: _Ctx, argv: list[str], stdin) -> subprocess.Popen:
    return subprocess.Popen(sandbox.wrap(ctx.sb, argv, ctx.work, ctx.harness, ctx.cwd_rel, ctx.env), shell=False,
                            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
                            cwd="/", start_new_session=True, preexec_fn=sandbox.preexec)


def _run_capped(ctx: _Ctx, argv: list[str]) -> tuple[int | None, bytes, bytes, bool]:
    """Run a sandboxed command; output is capped (excess drained and dropped). Returns (rc, out, err, timed_out)."""
    p = _spawn(ctx, argv, subprocess.DEVNULL)
    out: list = []
    err: list = []
    ts = [threading.Thread(target=_drain, args=(p.stdout, out, MAX_OUTPUT), daemon=True),
          threading.Thread(target=_drain, args=(p.stderr, err, MAX_OUTPUT), daemon=True)]
    for t in ts:
        t.start()
    timed_out = False
    try:
        rc = p.wait(timeout=ctx.timeout)
    except subprocess.TimeoutExpired:
        timed_out, rc = True, None
        _kill(p)
        p.wait()
    _kill(p)  # nothing from the sandbox outlives the run
    for t in ts:
        t.join(timeout=5)
    return rc, b"".join(out), b"".join(err), timed_out


def _text(b: bytes) -> str:
    return redact(b[:MAX_OUTPUT].decode("utf-8", "replace"))


def _run_exit_code_only(prof, ctx: _Ctx, ph, eid) -> VerifyOutcome:
    rc, out, err, to = _run_capped(ctx.fresh("x"), [ctx.exe, *prof["argv"][1:]])
    if to:
        return VerifyOutcome("failed", None, True, _text(out), _text(err), "timeout (not success)", ph, eid,
                             prof["argv"])
    status = "passed" if rc == 0 else "failed"
    return VerifyOutcome(status, rc, False, _text(out), _text(err),
                         f"assurance=cooperative: exit {rc} (exit-code-only profile, sandboxed; the exit code is "
                         f"forgeable by the code under test; the final gate requires an accepted code_review and qa "
                         f"from Bots outside the executor's independence group, unless reduced assurance was "
                         f"explicitly accepted)", ph, eid, prof["argv"], "cooperative")


def _py_argv(ctx: _Ctx, script: Path) -> list[str]:
    """System interpreter inside the sandbox: -I (isolated) -S (no site-packages at all) -B, throwaway pycache."""
    return [ctx.exe, "-I", "-S", "-B", "-X", "pycache_prefix=/tmp/omo-pyc", f"{sandbox.SANDBOX_HARNESS}/{script.name}"]


REPORT_PREFIX = "OMO-UNITTEST-REPORT "


def evaluate_report(rep: dict | None, returncode: int | None, prof: dict) -> list[str]:
    """Why the (untrusted, advisory) unittest child report is NOT clean. Empty list = the child CLAIMS a clean run."""
    why = []
    if returncode != 0:
        why.append(f"unittest child exit {returncode}")
    if not isinstance(rep, dict):
        why.append("no unittest report (process exited before reporting, or the report was unparseable)")
        return why
    if rep.get("stage") != "done":
        why.append(f"unittest child stopped at stage {str(rep.get('stage'))[:40]!r}")
    if rep.get("tamper"):
        why.append("test machinery tampered (child self-report)")
    if rep.get("load_errors"):
        why.append("test load errors")
    expected = list(prof["expected_tests"])
    tests = rep.get("tests") if isinstance(rep.get("tests"), dict) else {}
    missing = [t for t in expected if t not in tests]
    if missing:
        why.append(f"expected tests not run: {missing}")
    notpass = sorted(str(t)[:200] for t, v in tests.items() if v != "passed")
    if notpass:
        why.append(f"tests not passed: {notpass[:10]}")
    extra = sorted(str(t)[:200] for t in set(tests) - set(expected))
    if extra:
        why.append(f"unexpected tests ran: {extra[:10]}")
    n = len(expected)
    want = {"run": n, "started": n, "failures": 0, "errors": 0, "skipped": 0, "expected_failures": 0,
            "unexpected_successes": 0}
    if rep.get("counts") != want:
        why.append(f"counts != expected {want}")
    return why


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_json_untrusted(data: bytes | str, limit: int = MAX_LINE):
    """Parse one JSON document from an untrusted child. Any failure (oversize, bad UTF-8, malformed, too deep,
    out of memory, NaN/Infinity) returns None: callers fail closed."""
    if len(data) > limit:
        return None
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError, MemoryError, TypeError):
        return None


def _parse_report(stdout: bytes | str):
    if isinstance(stdout, bytes):
        stdout = stdout[:MAX_OUTPUT].decode("utf-8", "replace")
    for line in reversed(stdout[:MAX_OUTPUT].splitlines()):
        if line.startswith(REPORT_PREFIX):
            rep = parse_json_untrusted(line[len(REPORT_PREFIX):])
            return rep if isinstance(rep, dict) else None
    return None


def _run_unittest_child(prof, ctx: _Ctx):
    ctx = ctx.fresh("u")
    rc, out, err, to = _run_capped(ctx, [*_py_argv(ctx, UNITTEST_CHILD), prof["start_dir"]])
    if to:
        return rc, _text(out), _text(err), ["unittest child timeout (not success)"], True
    return rc, _text(out), _text(err), evaluate_report(_parse_report(out), rc, prof), False


class _Proto(Exception):
    pass


ORACLE_RUNS = 3  # separate oracle runs per verification, each under a different neutral environment


def _alias_ok(alias: str, exe: str) -> bool:
    return os.path.realpath(alias if "/" in alias else f"/usr/bin/{alias}") == exe


def oracle_variants(prof, ctx: _Ctx, rng) -> list[dict]:
    """Deliberately varied, neutral environments for the oracle runs (user decision H1). Nothing in them names the
    harness: random ordinary user/home/project/hostname, plain `-c` import, no script path. Flags are OFF in the plain
    run and ON in the isolated run (-s -S -B -P, not -I, so the per-run PYTHONHASHSEED is honoured), the third looks
    like an interactive session; each run has its own explicit hash seed; argv[0], env sets, HOME, cwd and the
    call-stack depth differ between runs. A candidate must be correct in every run, with identical results."""
    names = ("python3", "/usr/bin/python3", ctx.exe)
    plain0 = next((a for a in names if _alias_ok(a, ctx.exe)), ctx.exe)
    other0 = next((a for a in reversed(names) if _alias_ok(a, ctx.exe) and a != plain0), ctx.exe)
    extra = {k: os.environ[k] for k in prof.get("env_allow", []) if k in os.environ}
    extra.update({k: str(v) for k, v in prof.get("env_set", {}).items()})
    lays: list = []
    while len(lays) < ORACLE_RUNS:
        lay = sandbox.neutral_layout(rng)
        if all(lay.project != x.project and lay.home != x.home for x in lays):
            lays.append(lay)
    base = [sandbox.login_env(x) for x in lays]
    # (round 6, C2-2) a distinct, EFFECTIVE hash seed per run: two explicit distinct seeds, and the plain run's own
    # random seed; no run uses -I/-E (which would ignore PYTHON* variables): the isolated run gets the same
    # isolation from -s -S -B -P instead. Detection of hash-order
    # dependence this way is probabilistic; the pure rule's set/frozenset/hash ban is the enforcement.
    seeds: list[int] = []
    while len(seeds) < 2:
        x = rng.randrange(1, 1 << 32)
        if x not in seeds:
            seeds.append(x)
    runs = [
        # plain: no PYTHONHASHSEED at all (the interpreter draws its own random seed, as in a normal session)
        {"name": "plain", "argv0": plain0, "flags": [], "env": base[0], "pad": 0, "hard": False, "cwd": False},
        {"name": "isolated-flags", "argv0": other0, "flags": ["-s", "-S", "-B", "-P"],
         "env": {"PATH": "/usr/bin:/bin", "HOME": lays[1].home, "PYTHONHASHSEED": str(seeds[0])}, "pad": 150,
         "hard": False, "cwd": True},
        {"name": "interactive-like", "argv0": plain0, "flags": ["-q", "-i"],
         "env": dict(base[2], LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1", EDITOR="vi",
                     PYTHONHASHSEED=str(seeds[1])), "pad": 300, "hard": True, "cwd": False},
    ]
    # address-space limits differ per run (low / default / none), assigned at random to the variants
    limits = [rng.randrange(256, 513) << 20, sandbox.RLIMITS[resource.RLIMIT_AS], None]
    rng.shuffle(limits)
    for r, lay, lim in zip(runs, lays, limits):
        r["layout"] = lay
        r["env"] = dict(r["env"], **extra)
        r["as_limit"] = lim
    rng.shuffle(runs)
    return runs


def run_oracle(prof, ctx: _Ctx) -> tuple[list[str], int, str]:
    """Trusted decision. The parent never imports candidate code; it draws fresh random inputs and runs the
    candidate ORACLE_RUNS times, each in a fresh sandboxed copy under a different neutral environment. Inputs are sent
    ONE AT A TIME (the next input does not exist until the previous answer arrives); every expected value is
    recomputed by the parent; every run must match, and the runs must agree exactly with each other. Returns
    (failure reasons, cases matched in total, child stderr). Every parse/decode/size/depth problem fails closed."""
    spec = prof["oracle"]
    cases = oracles.cases(spec["name"], spec.get("cases_per_function", 48))
    mods = sorted({c["call"].rsplit(".", 1)[0] for c in cases})
    rng = random.SystemRandom()
    why: list[str] = []
    errs: list[str] = []
    total = 0
    seen: dict[int, str] = {}
    next_id = len(cases)
    for v in oracle_variants(prof, ctx, rng):
        # shared cases (compared across runs) + this run's own fresh large-band cases (round 8; never reused)
        own = oracles.run_cases(spec["name"], next_id)
        next_id += len(own)
        order = list(cases) + own
        rng.shuffle(order)
        w, matched, err, values = _oracle_session(ctx, v, order, mods)
        total += matched
        if err:
            errs.append(f"[run {v['name']}] {err}")
        if w:
            why += [f"{x} [run {v['name']}]" for x in w]
            break
        for cid, val in values.items():
            if cid >= len(cases):
                continue  # this run's own cases: checked against the oracle only
            if seen.setdefault(cid, val) != val:
                why.append(f"result for case {cid} differs from an earlier run (environment-dependent "
                           f"behaviour) [run {v['name']}]")
                break
        if why:
            break
    return why, total, "\n".join(errs)


def _oracle_session(ctx: _Ctx, v: dict, cases: list, mods: list[str]):
    c2 = ctx.fresh("r")
    lay = v["layout"]
    etc = sandbox.write_etc(lay, c2.staging / f"etc-{uuid.uuid4().hex[:6]}")
    code = oracle_child.bootstrap(mods, v["pad"], v["hard"], v["cwd"])
    argv = sandbox.wrap_neutral(ctx.sb, [v["argv0"], *v["flags"], "-c", code], c2.work, etc, lay, v["env"],
                                ctx.cwd_rel)
    deadline = time.monotonic() + ctx.timeout
    p = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={"PATH": "/usr/bin:/bin"}, start_new_session=True,
                         preexec_fn=sandbox.preexec_with(v.get("as_limit", sandbox.RLIMITS[resource.RLIMIT_AS])))
    err: list = []
    et = threading.Thread(target=_drain, args=(p.stderr, err, MAX_OUTPUT), daemon=True)
    et.start()
    lines: queue.Queue = queue.Queue()

    def pump():
        total = 0
        try:
            while True:
                ln = p.stdout.readline(MAX_LINE + 1)
                if not ln:
                    break
                total += len(ln)
                if len(ln) > MAX_LINE or total > MAX_PROTOCOL:
                    lines.put(b"\x00OVERSIZE")
                    break
                lines.put(ln)
        except (OSError, ValueError):
            pass
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
        if ln == b"\x00OVERSIZE":
            raise _Proto("oversized output")
        msg = parse_json_untrusted(ln)
        if not isinstance(msg, dict):
            raise _Proto("unparseable/oversized/too-deep or non-object message")
        return msg

    def send(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode())
        p.stdin.flush()

    why: list[str] = []
    values: dict[int, str] = {}
    matched = 0
    rc = None
    try:
        if recv() != {"ready": True}:
            why.append("oracle child did not report ready")
        for c in cases if not why else []:
            send(oracles.wire(c))
            msg = recv()
            if set(msg) != {"id", "value"} or type(msg["id"]) is not int or msg["id"] != c["id"]:
                why.append(f"oracle case {c['id']} ({c['call']}): bad/forged reply")
                break
            if not oracles.check_value(c, msg["value"]):
                why.append(f"oracle case {c['id']} ({c['call']}): wrong value or type "
                           f"(got {type(msg['value']).__name__})")
                break
            values[c["id"]] = f"{type(msg['value']).__name__}:{msg['value']!r}"
            matched += 1
        if not why:
            send({"end": True})
    except (TimeoutError, queue.Empty):
        why.append("oracle timeout (not success)")
    except EOFError:
        why.append(f"oracle child exited after {matched}/{len(cases)} cases (exit/forgery cannot pass)")
    except _Proto as e:
        why.append(f"oracle protocol violation after {matched}/{len(cases)} cases: {e}")
    except (ValueError, KeyError, TypeError, RecursionError, MemoryError, OSError) as e:
        why.append(f"oracle protocol violation after {matched}/{len(cases)} cases: {type(e).__name__}")
    finally:
        if why:  # failed already: do not wait on a child that may be blocked writing or spinning
            _kill(p)
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            rc = p.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill(p)
            rc = p.wait()
        _kill(p)
        et.join(timeout=5)
    if not why and matched != len(cases):
        why.append(f"only {matched}/{len(cases)} oracle cases matched")
    if not why and rc != 0:
        why.append(f"oracle child exit {rc}")
    return why, matched, b"".join(err).decode("utf-8", "replace"), values


def _run_python(prof, ctx: _Ctx, ph, eid) -> VerifyOutcome:
    assurance = profile_assurance(prof)
    rc, out, err, adv, to = _run_unittest_child(prof, ctx)
    if to:
        return VerifyOutcome("failed", None, True, out, err, "timeout (not success)", ph, eid, prof["argv"],
                             assurance)
    if assurance == "oracle":
        why, matched, oerr = run_oracle(prof, ctx)
        err = err + ("\n--- oracle child stderr ---\n" + redact(oerr) if oerr else "")
        reasons = [f"ORACLE: {w}" for w in why] + [f"unittest (advisory, negative evidence still fails): {w}"
                                                    for w in adv]
        if reasons:
            return VerifyOutcome("failed", rc, False, out, err, "assurance=oracle: " + "; ".join(reasons), ph, eid,
                                 prof["argv"], assurance)
        return VerifyOutcome("passed", 0, False, out, err,
                             f"assurance=oracle: {matched}/{matched} oracle cases (exhaustive small grid + boundaries, "
                             f"positional/keyword/mixed calls, aliased equal arguments, bools, fresh random draws incl. "
                             f"{oracles.LARGE_PER_RUN} fresh large-magnitude draws per function per run) "
                             f"matched across {ORACLE_RUNS} varied neutral sandboxed runs with "
                             f"different memory limits (parent-computed, identical results); unittest child (advisory self-report) claims "
                             f"{len(prof['expected_tests'])} expected tests passed", ph, eid, prof["argv"], assurance)
    if adv:
        return VerifyOutcome("failed", rc, False, out, err, "assurance=cooperative: " + "; ".join(adv), ph, eid,
                             prof["argv"], assurance)
    return VerifyOutcome("passed", 0, False, out, err,
                         f"assurance=cooperative: unittest child self-reports {len(prof['expected_tests'])} expected "
                         f"tests passed (sandboxed, but forgeable by the code under test; the final gate requires an "
                         f"accepted code_review and qa from Bots outside the executor's independence group, unless "
                         f"reduced assurance was explicitly accepted)", ph, eid, prof["argv"], assurance)
