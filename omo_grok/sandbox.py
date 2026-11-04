"""Filesystem/network sandbox for every process that runs candidate code.

Mechanism: bubblewrap (`bwrap`, unprivileged user + mount + pid + net + ipc + uts + cgroup namespaces). No root, no
sudo, no extra OS users. The sandboxed process sees ONLY:
  /usr (+ /bin, /lib, /lib64, /sbin symlinks)  read-only bind of the system runtime (system python + its stdlib);
                                               /usr/local is hidden behind an empty read-only tmpfs
  /omo-harness                                 read-only, a private per-run COPY of the child scripts
  /work                                        writable, a private per-run COPY of the candidate checkout (no .git)
  /tmp                                         private tmpfs;   /proc (own pid namespace), minimal /dev
It has no view of OMO_HOME/state (DB, secrets), omo_grok/, the venv, /home, or any other /workspace content, and no
network (empty net namespace). The unittest child runs as `<system python> -I -S -B` (no site-packages). Oracle runs
use `wrap_neutral`: the checkout copy is bound at a random ordinary project path with an ordinary HOME, a synthetic
read-only /etc and hostname, and no harness markers (user decision H1; see docs/SECURITY.md).

If bwrap is missing or cannot create namespaces, `probe()` reports why and the verifier FAILS CLOSED; candidate code
is never run unsandboxed. There is no configuration switch that disables the sandbox."""
from __future__ import annotations

import os
import re
import resource
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BWRAP_CANDIDATES = ("/usr/bin/bwrap", "/bin/bwrap")
BWRAP_VERSION_RE = re.compile(r"bubblewrap (\d{1,3}\.\d{1,3}(?:\.\d{1,3})?)")
RUNTIME_ROOT = "/usr"
SANDBOX_WORK = "/work"
SANDBOX_HARNESS = "/omo-harness"
RLIMITS = {resource.RLIMIT_AS: 4 << 30, resource.RLIMIT_FSIZE: 256 << 20, resource.RLIMIT_CORE: 0}


@dataclass(frozen=True)
class Probe:
    ok: bool
    mechanism: str  # "bwrap" | "none"
    detail: str
    bwrap: str | None = None
    version: str | None = None  # e.g. "0.12.0", parsed from `bwrap --version`

    @property
    def label(self) -> str:
        """What is recorded in verifications.sandbox, e.g. "bwrap 0.12.0" (or "none")."""
        return f"{self.mechanism} {self.version}" if self.ok and self.version else self.mechanism


def _bwrap_path() -> str | None:
    for p in BWRAP_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


@lru_cache(maxsize=1)
def probe() -> Probe:
    bw = _bwrap_path()
    if not bw:
        return Probe(False, "none", "no sandbox available: bwrap (bubblewrap) not installed")
    try:
        vp = subprocess.run([bw, "--version"], capture_output=True, text=True, timeout=10)
        m = BWRAP_VERSION_RE.fullmatch(vp.stdout.strip()) if vp.returncode == 0 else None
        if not m:
            return Probe(False, "none", "no sandbox available: unexpected `bwrap --version` output "
                         f"(exit {vp.returncode}): {vp.stdout.strip()[:60]!r}")
        cp = subprocess.run([bw, *base_args(), "--", "/usr/bin/true"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return Probe(False, "none", f"no sandbox available: bwrap failed to start ({type(e).__name__})")
    if cp.returncode != 0:
        return Probe(False, "none", "no sandbox available: bwrap cannot create user/mount/net namespaces: "
                     + cp.stderr.strip()[:200])
    return Probe(True, "bwrap", f"bubblewrap {m.group(1)}; userns+mountns+pidns+netns; kernel {os.uname().release}",
                 bw, m.group(1))


def base_args() -> list[str]:
    return ["--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", RUNTIME_ROOT, RUNTIME_ROOT, "--tmpfs", "/usr/local", "--remount-ro", "/usr/local",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--clearenv"]


def runtime_executable(exe: str) -> str | None:
    """The real path of an interpreter/command if it lives inside the read-only runtime, else None."""
    real = os.path.realpath(exe)
    ok = real.startswith(RUNTIME_ROOT + "/") and not real.startswith("/usr/local/") and os.path.isfile(real)
    return real if ok else None


def stage(checkout: Path, scripts: list[Path], into: Path) -> tuple[Path, Path]:
    """Fresh per-run copies: the checkout (without .git) as writable scratch, and the child scripts read-only."""
    work = into / "work"
    shutil.copytree(checkout, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    harness = into / "harness"
    harness.mkdir()
    for s in scripts:
        shutil.copy2(s, harness / s.name)
        os.chmod(harness / s.name, 0o444)
    os.chmod(harness, 0o555)
    return work, harness


def wrap(pr: Probe, argv: list[str], work: Path, harness: Path | None, cwd_rel: str, env: dict) -> list[str]:
    out = [pr.bwrap or "bwrap", *base_args(), "--bind", str(work), SANDBOX_WORK]
    if harness is not None:
        out += ["--ro-bind", str(harness), SANDBOX_HARNESS]
    rel = os.path.normpath(cwd_rel or ".")
    out += ["--chdir", SANDBOX_WORK if rel == "." else f"{SANDBOX_WORK}/{rel}"]
    for k, v in sorted(env.items()):
        out += ["--setenv", k, v]
    return [*out, "--", *argv]


def _apply_rlimits(limits: dict) -> None:
    for k, v in limits.items():
        try:
            resource.setrlimit(k, (v, v))
        except (ValueError, OSError):
            pass


def preexec() -> None:  # runs in the forked child before exec of bwrap
    _apply_rlimits(RLIMITS)


def preexec_with(address_space: int | None):
    """Same limits as preexec() but with a different RLIMIT_AS (None = RLIM_INFINITY). The oracle runs use a LOW,
    the DEFAULT and NO address-space limit, so code that senses the memory limit cannot behave identically in all
    three runs (results must agree), while ordinary code is unaffected. FSIZE/CORE limits, the bwrap namespaces, the
    wall-clock timeout and the output caps still apply to every run."""
    limits = dict(RLIMITS)
    limits[resource.RLIMIT_AS] = resource.RLIM_INFINITY if address_space is None else address_space

    def fn() -> None:
        _apply_rlimits(limits)
    return fn


# --- neutral layouts for the oracle runs (user decision H1: harness detection is not an accepted residual) ---------
# Each oracle run binds the checkout copy at a randomly chosen, ordinary-looking project path under an ordinary home
# directory, with an ordinary /etc/passwd entry and hostname. None of these names contain harness markers.
NEUTRAL_USERS = ("alex", "sam", "jordan", "casey", "robin", "dana", "kim", "lee")
NEUTRAL_PROJECTS = ("calc", "calculator", "mathlib", "project", "src", "code", "lib", "app")
NEUTRAL_HOSTS = ("laptop", "devbox", "workstation", "desktop", "ubuntu", "dev", "home-pc", "thinkpad")
HARNESS_MARKERS = ("omo", "verif", "harness", "oracle", "sandbox", "bwrap", "grok", "probe")


@dataclass(frozen=True)
class Layout:
    user: str
    home: str
    project: str
    hostname: str


def neutral_layout(rng) -> Layout:
    user = rng.choice(NEUTRAL_USERS)
    home = f"/home/{user}"
    base = rng.choice([home, f"{home}/code", f"{home}/projects", "/srv", "/opt"])
    lay = Layout(user, home, f"{base}/{rng.choice(NEUTRAL_PROJECTS)}", rng.choice(NEUTRAL_HOSTS))
    assert not any(m in x.lower() for m in HARNESS_MARKERS for x in (lay.home, lay.project, lay.hostname))
    return lay


def write_etc(lay: Layout, into: Path) -> Path:
    etc = into
    etc.mkdir(parents=True)
    uid, gid = os.getuid(), os.getgid()
    (etc / "passwd").write_text(f"root:x:0:0:root:/root:/bin/bash\n{lay.user}:x:{uid}:{gid}:{lay.user}:{lay.home}:"
                                f"/bin/bash\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n")
    (etc / "group").write_text(f"root:x:0:\n{lay.user}:x:{gid}:\nnogroup:x:65534:\n")
    (etc / "hostname").write_text(lay.hostname + "\n")
    (etc / "hosts").write_text(f"127.0.0.1 localhost\n127.0.1.1 {lay.hostname}\n")
    return etc


def login_env(lay: Layout) -> dict:
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": lay.home,
            "USER": lay.user, "LOGNAME": lay.user, "SHELL": "/bin/bash", "LANG": "C.UTF-8", "TERM": "xterm-256color"}


def wrap_neutral(pr: Probe, argv: list[str], work: Path, etc: Path, lay: Layout, env: dict,
                 cwd_rel: str = ".") -> list[str]:
    rel = os.path.normpath(cwd_rel or ".")
    out = [pr.bwrap or "bwrap", *base_args(), "--ro-bind", str(etc), "/etc", "--hostname", lay.hostname,
           "--tmpfs", lay.home, "--bind", str(work), lay.project,
           "--chdir", lay.project if rel == "." else f"{lay.project}/{rel}"]
    for k, v in sorted(env.items()):
        out += ["--setenv", k, v]
    return [*out, "--", *argv]
