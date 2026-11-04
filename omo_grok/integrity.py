"""Tamper tripwire around every verification (defence in depth behind the sandbox).

Before and after each run the verifier hashes the trusted surfaces a candidate must never change: the venv
site-packages (including .pth files and __pycache__), omo_grok/ (including __pycache__), the code-root config and the
active config dir, and omoctl. Any added, removed or modified file fails the verification and raises a `tamper`
event. False positives (e.g. an operator editing code during a run) fail closed, by design."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .paths import CODE_ROOT, get_paths


def surfaces() -> list[Path]:
    venv = CODE_ROOT / ".venv"
    out = [CODE_ROOT / "omo_grok", CODE_ROOT / "config", CODE_ROOT / "omoctl", get_paths().config_dir,
           venv / "bin", venv / "pyvenv.cfg"]
    out += sorted((CODE_ROOT / ".venv" / "lib").glob("python*/site-packages"))
    seen, uniq = set(), []
    for p in out:
        r = os.path.realpath(p)
        if r not in seen:
            seen.add(r)
            uniq.append(Path(r))
    return uniq


def _file_digest(p: Path) -> str:
    if p.is_symlink():
        return "link:" + os.readlink(p)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(roots: list[Path] | None = None) -> dict[str, str]:
    snap: dict[str, str] = {}
    for root in roots or surfaces():
        if root.is_file() or root.is_symlink():
            snap[str(root)] = _file_digest(root)
            continue
        if not root.exists():
            snap[str(root)] = "missing"
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for n in sorted(filenames):
                p = Path(dirpath) / n
                try:
                    snap[str(p)] = _file_digest(p)
                except OSError as e:
                    snap[str(p)] = f"unreadable:{type(e).__name__}"
            for n in dirnames:
                d = Path(dirpath) / n
                if d.is_symlink():
                    snap[str(d)] = "link:" + os.readlink(d)
    return snap


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    out = []
    for k in sorted(set(before) | set(after)):
        if k not in after:
            out.append(f"removed {k}")
        elif k not in before:
            out.append(f"added {k}")
        elif before[k] != after[k]:
            out.append(f"modified {k}")
    return out
