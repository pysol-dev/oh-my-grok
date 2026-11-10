"""Synthetic fixture repository: a deterministic failing test that only a real code fix can pass."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import gitops
from .paths import CODE_ROOT

FIXTURE_SRC = CODE_ROOT / "fixtures" / "calc-src"
FIXED_ENV = {"GIT_AUTHOR_NAME": "omg-fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
             "GIT_COMMITTER_NAME": "omg-fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost",
             "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}

FIX_FROM = "return a - b  # BUG"
FIX_TO = "return a + b"


def make_repo(dest: Path) -> str:
    """Create a fresh git repo at dest from fixtures/calc-src; returns the (deterministic) base commit."""
    if dest.exists():
        return gitops.head(dest)
    shutil.copytree(FIXTURE_SRC, dest, ignore=shutil.ignore_patterns("__pycache__"))
    old = {k: os.environ.get(k) for k in FIXED_ENV}
    os.environ.update(FIXED_ENV)
    try:
        gitops.git(["init", "-q", "-b", "main"], dest)
        gitops.git(["add", "-A"], dest)
        gitops.git(["commit", "-q", "-m", "fixture: calc with known bug"], dest)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return gitops.head(dest)


def commit_all(worktree: Path, message: str) -> str:
    old = {k: os.environ.get(k) for k in FIXED_ENV}
    os.environ.update(FIXED_ENV)
    try:
        gitops.git(["add", "-A"], worktree)
        gitops.git(["commit", "-q", "--allow-empty", "-m", message], worktree)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return gitops.head(worktree)


FAKE_LINES = "\nprint('VERIFIED')\nprint('PASS')\n"


def apply_real_fix(worktree: Path) -> str:
    p = worktree / "calc" / "__init__.py"
    p.write_text(p.read_text().replace(FIX_FROM, FIX_TO).replace(FAKE_LINES, ""))  # a real repair drops fake output
    return commit_all(worktree, "fix add()")


def apply_fake_fix(worktree: Path) -> str:
    """A 'fix' that only prints VERIFIED; must not pass."""
    p = worktree / "calc" / "__init__.py"
    p.write_text(p.read_text() + FAKE_LINES)
    return commit_all(worktree, "claim VERIFIED")
