"""Git operations for per-job worktrees. Repo hooks are disabled; never reset/clean/force/delete user branches."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .errors import GateRefused, ValidationError

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def git(args: list[str], cwd: Path | str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LC_ALL": "C",
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
              "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        if k in os.environ:
            env[k] = os.environ[k]
    cp = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(cwd), env=env, shell=False,
                        capture_output=True, text=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise GateRefused(f"git {args[0]} failed: {cp.stderr.strip()[:500]}")
    return cp


def toplevel(path: Path) -> Path:
    cp = git(["rev-parse", "--show-toplevel"], path, check=False)
    if cp.returncode != 0:
        raise ValidationError(f"not a git repository: {path}")
    return Path(cp.stdout.strip()).resolve()


def head(path: Path) -> str:
    return git(["rev-parse", "HEAD"], path).stdout.strip()


def commit_exists(repo: Path, sha: str) -> bool:
    if not SHA40.match(sha or ""):
        return False
    return git(["cat-file", "-e", f"{sha}^{{commit}}"], repo, check=False).returncode == 0


def is_ancestor(repo: Path, base: str, sha: str) -> bool:
    return git(["merge-base", "--is-ancestor", base, sha], repo, check=False).returncode == 0


def branch_head(repo: Path, branch: str) -> str | None:
    cp = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False)
    return cp.stdout.strip() if cp.returncode == 0 else None


def changed_files(repo: Path, base: str, sha: str) -> list[str]:
    """All paths added/modified/deleted/renamed (both sides) between base and sha; NUL-separated, no rename folding."""
    out = git(["diff", "--name-only", "--no-renames", "-z", base, sha], repo).stdout
    return [p for p in out.split("\0") if p]


def changed_entries(repo: Path, base: str, sha: str) -> list[tuple[str, str, str]]:
    """(status, new_mode, path) for every change base..sha; renames are split into delete + add."""
    out = git(["diff", "--raw", "--no-renames", "-z", base, sha], repo).stdout
    parts = [p for p in out.split("\0")]
    res = []
    i = 0
    while i < len(parts) - 1:
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        fields = meta[1:].split()
        res.append((fields[4][0], fields[1], parts[i + 1]))
        i += 2
    return res


def create_worktree(repo: Path, dest: Path, branch: str, base: str) -> None:
    """Idempotent: reuses an existing worktree at dest on the same branch; refuses to clobber anything else."""
    if dest.exists():
        if (dest / ".git").exists() and git(["rev-parse", "--abbrev-ref", "HEAD"], dest).stdout.strip() == branch:
            return
        raise GateRefused(f"worktree path exists and is not this job's worktree: {dest}")
    if branch_head(repo, branch) is not None:
        raise GateRefused(f"branch {branch} already exists; refusing to reuse or reset it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(dest), base], repo)


def temp_checkout(repo: Path, sha: str, dest: Path) -> None:
    git(["worktree", "add", "--detach", str(dest), sha], repo)


def remove_temp_checkout(repo: Path, dest: Path) -> None:
    # only ever used on our own detached verification checkouts under jobs/<id>/verify/
    git(["worktree", "remove", "--force", str(dest)], repo, check=False)


def history_shape(repo: Path, base: str, sha: str, job_id: str, max_commits: int) -> dict:
    """Structural facts about base..sha that git itself can prove: linear, bounded, trailer on every commit."""
    rows = [r.split() for r in git(["rev-list", "--parents", f"{base}..{sha}"], repo).stdout.splitlines() if r.strip()]
    merges = [r[0] for r in rows if len(r) > 2]
    trailer = re.compile(rf"^Omo-Job: {re.escape(job_id)}[ \t]*$", re.MULTILINE)
    missing = []
    raw = git(["log", "--format=%H%x1f%B%x1e", f"{base}..{sha}"], repo).stdout
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        commit, _, body = rec.strip("\n").partition("\x1f")
        if not trailer.search(body):
            missing.append(commit.strip())
    return {"commits": len(rows), "max_commits": max_commits, "merge_commits": merges, "missing_trailer": missing,
            "ok": bool(rows) and not merges and not missing and len(rows) <= max_commits}
