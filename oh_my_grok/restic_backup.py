"""Restic backups of omg core state onto a local repository directory.

The repository usually lives on the agent store. That filesystem rejects chmod, utime, hardlinks,
symlinks and mtime-preserving rename, and it refuses any file at or above 16 MiB. Staging and
restore therefore happen under /tmp. Restic itself is invoked with a 4 MiB pack size so the packs
it writes stay under the cap. Nothing in this module chmods, utimes, or links a path inside the
repository.

The repository password is read from OMG_RESTIC_PASSWORD and handed to restic only through
RESTIC_PASSWORD_COMMAND='printenv OMG_RESTIC_PASSWORD'. It is never placed on argv, never written
to a file, and never included in an event. Hook bytes are copied into the snapshot as raw bytes
and are not returned, logged, or stored in an event.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db, features
from .config import canonical_json, load_policy, sha256_text
from .errors import IntegrityError_, OmgError
from .paths import Paths, get_paths, tools_dir
from .redact import redact, register_secret, scrubbed_environ

PACK_MIB = 4
HOST = "omg-box"
PASSWORD_COMMAND = "printenv OMG_RESTIC_PASSWORD"
PASSWORD_ENV = "OMG_RESTIC_PASSWORD"
# Measured store cap. One byte over fails with EFBIG, including a temp file that is then renamed.
STORE_FILE_CAP = 16 * 1024 * 1024
# Doctor fails a file before it can reach that cap.
DOCTOR_FILE_LIMIT = 15 * 1024 * 1024
LOCK_YOUNG_SECONDS = 30 * 60
RETRY_LOCK = "2m"
EVENT_FIELDS = ("id", "ts", "job_id", "task_id", "kind", "actor", "correlation_id", "data_json")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_SNAPSHOT = 10
EXIT_DIVERGED = 11
EXIT_VERIFY = 12
EXIT_PASSWORD = 13
EXIT_CHECK_BEHIND = 14
EXIT_POST = 15
# Restic itself exits 11 when the repository is locked. omg reports that as 16 so it is not
# the same code as a diverged restore.
EXIT_LOCK = 16
EXIT_LOCKED = EXIT_LOCK

Runner = Callable[[list[str], dict[str, str], float], "CommandResult"]


class BackupFailed(OmgError):
    """A backup, prune, or check failed. ``reason`` is safe to log; it is never the password."""

    code = "backup_failed"

    def __init__(self, reason: str, **details):
        self.reason = reason
        self.exit_code = {
            "password_unset": EXIT_USAGE,
            "password_rejected": EXIT_PASSWORD,
            "locked": EXIT_LOCKED,
            "repo_unset": EXIT_USAGE,
            "restic_missing": EXIT_USAGE,
        }.get(reason, 1)
        super().__init__(f"backup failed: {reason}", reason=reason, **details)


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    efbig: bool = False
    locked: bool = False
    timed_out: bool = False


def password_is_set() -> bool:
    return bool(os.environ.get(PASSWORD_ENV))


def require_password() -> str:
    password = os.environ.get(PASSWORD_ENV) or ""
    if not password:
        raise BackupFailed("password_unset")
    register_secret(password, min_length=1)
    return password


def repo_path(policy: dict) -> Path | None:
    raw = policy.get("backup_repo") or ""
    if not isinstance(raw, str) or raw == "":
        return None
    return Path(raw)


def restic_binary() -> Path:
    override = os.environ.get("OMG_RESTIC_BIN")
    if override:
        return Path(override)
    candidate = tools_dir() / "restic"
    if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("restic")
    if found:
        return Path(found)
    raise BackupFailed("restic_missing")


def backup_due(*, last_at: float | None, last_gen: int | None, gen: int, now: float,
               min_interval: float, max_interval: float) -> bool:
    """True when nothing has been saved yet, the generation changed and the short interval elapsed,
    or the long interval elapsed whether or not the generation changed."""
    if last_at is None:
        return True
    age = now - float(last_at)
    if age >= float(max_interval):
        return True
    previous = -1 if last_gen is None else int(last_gen)
    return int(gen) != previous and age >= float(min_interval)


def _efbig_backoff(status: dict[str, Any], now: float, policy: dict, *,
                   reason_key: str = "last_failure_reason", attempt_key: str = "last_attempt_unix",
                   success_key: str = "last_backup_unix",
                   max_interval_key: str = "backup_max_interval_seconds") -> bool:
    """An EFBIG failure is not retried on the next tick. The long ceiling still runs the operation."""
    if status.get(reason_key) != "efbig":
        return False
    attempt = status.get(attempt_key)
    if not isinstance(attempt, (int, float)) or isinstance(attempt, bool):
        return False
    if now - float(attempt) >= float(policy["backup_min_interval_seconds"]):
        return False
    last_ok = status.get(success_key)
    forced = (isinstance(last_ok, (int, float)) and not isinstance(last_ok, bool)
              and now - float(last_ok) >= float(policy[max_interval_key]))
    return not forced


def staging_root(paths: Paths) -> Path:
    """One directory per home, reused so restic records the same path for every snapshot.

    A fresh temporary directory would make the default host+paths grouping keep every snapshot.
    """
    digest = hashlib.sha256(str(paths.root.resolve()).encode()).hexdigest()[:16]
    return Path("/tmp") / f"omg-backup-staging-{digest}"


def prepare_staging(paths: Paths) -> Path:
    root = staging_root(paths)
    if root.exists() or root.is_symlink():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def interval_due(last_at: float | None, now: float, interval: float) -> bool:
    if last_at is None:
        return True
    return now - float(last_at) >= float(interval)


# ---------------------------------------------------------------- generation (reused decision)
def event_stream(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, ts, job_id, task_id, kind, actor, correlation_id, data_json FROM events ORDER BY id ASC"
    ).fetchall()
    return [{field: row[field] for field in EVENT_FIELDS} for row in rows]


def generation_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    tail = events[-50:]
    return {"max_event_id": int(events[-1]["id"]) if events else 0,
            "tail_sha256": sha256_text(canonical_json(tail))}


def relate_streams(local_events: list[dict[str, Any]], backup_events: list[dict[str, Any]]) -> str:
    """equal, local_prefix (backup is a strict extension), local_newer, or diverged."""
    if local_events == backup_events:
        return "equal"
    if len(local_events) < len(backup_events) and backup_events[:len(local_events)] == local_events:
        return "local_prefix"
    if len(backup_events) < len(local_events) and local_events[:len(backup_events)] == backup_events:
        return "local_newer"
    return "diverged"


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def inspect_database(path: Path) -> tuple[str, list[dict[str, Any]] | None]:
    """Return missing, corrupt, or ok plus the event stream when the file is readable."""
    if not path.exists():
        return "missing", None
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return "corrupt", None
    # Do not let SQLite open a non-database: that rewrites the -shm sidecar before we can move it aside.
    if header != b"SQLite format 3\x00":
        return "corrupt", None
    try:
        conn = _open_ro(path)
    except sqlite3.Error:
        return "corrupt", None
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error:
            return "corrupt", None
        if row is None or row[0] != "ok":
            return "corrupt", None
        try:
            return "ok", event_stream(conn)
        except sqlite3.Error:
            return "corrupt", None
    finally:
        conn.close()


def schema_version(path: Path) -> int:
    conn = _open_ro(path)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        if not found:
            return 0
        value = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        return int(value or 0)
    finally:
        conn.close()


def current_generation(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(id) FROM events").fetchone()
    except sqlite3.Error:
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


# ---------------------------------------------------------------- staging (local /tmp only)
def _copy_tree(src: Path, dest: Path) -> None:
    if not src.exists():
        dest.mkdir(parents=True, exist_ok=True)
        return
    if src.is_symlink():
        raise IntegrityError_(f"refusing to archive symlink {src.name}")
    dest.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        kept = []
        for name in sorted(dirnames):
            child = current / name
            if child.is_symlink():
                raise IntegrityError_(f"refusing to archive symlink {(child.relative_to(src)).as_posix()}")
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(src)
            if path.is_symlink():
                raise IntegrityError_(f"refusing to archive symlink {rel.as_posix()}")
            try:
                data = path.read_bytes()
            except OSError:
                raise IntegrityError_(f"could not read {rel.as_posix()}") from None
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            mode = 0o600
            if path.stat().st_mode & stat.S_IXUSR:
                mode |= stat.S_IXUSR
            os.chmod(target, mode)
            del data


def _read_hooks(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise IntegrityError_("hooks.json must not be a symlink")
    try:
        data = path.read_bytes()
    except OSError:
        raise IntegrityError_("could not read hooks.json") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if len(text) >= 6:
        register_secret(text)
    return data


def _quiesce_sqlite(snapshot: Path) -> None:
    side = sqlite3.connect(str(snapshot))
    try:
        side.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        side.execute("PRAGMA journal_mode=DELETE")
    finally:
        side.close()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(snapshot) + suffix)
        if extra.is_symlink() or extra.exists():
            extra.unlink()


def stage_snapshot(conn: sqlite3.Connection, paths: Paths, root: Path) -> dict[str, Any]:
    """Write a consistent copy under ``root``. The returned dict does not contain hook bytes."""
    tree = root / "snapshot"
    db_name = paths.db_path.name
    if db_name not in ("omg.sqlite3", "omo.sqlite3"):
        raise IntegrityError_("refusing to archive an unexpected database name")
    snapshot = tree / "state" / db_name
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    db.backup(conn, snapshot)
    _quiesce_sqlite(snapshot)
    if paths.config_dir.is_symlink():
        raise IntegrityError_("refusing to archive symlink config")
    _copy_tree(paths.config_dir, tree / "config")
    _copy_tree(paths.jobs_dir, tree / "jobs")
    hooks = _read_hooks(paths.hooks_path)
    if hooks is not None:
        dest = tree / "state" / "secrets" / "hooks.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(hooks)
        os.chmod(dest, 0o600)
        del hooks
    health, events = inspect_database(snapshot)
    if health != "ok" or events is None:
        raise IntegrityError_("snapshot failed integrity_check")
    generation = generation_of(events)
    return {"tree": tree, "generation": int(generation["max_event_id"]),
            "tail_sha256": generation["tail_sha256"], "schema": schema_version(snapshot),
            "db_name": db_name}


def find_snapshot_root(tree: Path) -> Path:
    found: list[Path] = []
    if not tree.exists():
        raise IntegrityError_("snapshot tree is missing")
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        if current.name != "state":
            continue
        for name in filenames:
            path = current / name
            if name in ("omg.sqlite3", "omo.sqlite3") and path.is_file() and not path.is_symlink():
                found.append(current.parent)
    if len(found) != 1:
        raise IntegrityError_("snapshot must contain exactly one database")
    return found[0]


# ---------------------------------------------------------------- restic process
def _status_path(paths: Paths) -> Path:
    return paths.state_dir / "backup" / "status.json"


def load_status(paths: Paths) -> dict[str, Any]:
    path = _status_path(paths)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_status(paths: Paths, data: dict[str, Any]) -> None:
    path = _status_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def update_status(paths: Paths, **fields: Any) -> None:
    data = load_status(paths)
    data.update(fields)
    save_status(paths, data)


def _cache_dir(paths: Paths, repo: Path) -> Path:
    path = paths.state_dir / "restic-cache"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    try:
        resolved = path.resolve()
        repo_resolved = repo.resolve()
    except OSError:
        return path
    if resolved == repo_resolved or repo_resolved in resolved.parents or resolved in repo_resolved.parents:
        return Path(tempfile.mkdtemp(prefix="omg-restic-cache-", dir="/tmp"))
    return path


def _command_env(password: str, cache: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_REPOSITORY", "RESTIC_REPOSITORY_FILE",
                "RESTIC_PASSWORD_COMMAND"):
        env.pop(key, None)
    env["RESTIC_PASSWORD_COMMAND"] = PASSWORD_COMMAND
    env["RESTIC_PACK_SIZE"] = str(PACK_MIB)
    env[PASSWORD_ENV] = password
    env["RESTIC_CACHE_DIR"] = str(cache)
    return env


def _build_argv(binary: Path, repo: Path, cache: Path, command: list[str],
                extra_global: list[str] | None = None) -> list[str]:
    argv = [str(binary), "--repo", str(repo), "--pack-size", str(PACK_MIB), "--cache-dir", str(cache)]
    if extra_global:
        argv.extend(extra_global)
    argv.extend(command)
    return argv


def _is_efbig(text: str) -> bool:
    low = text.lower()
    return "efbig" in low or "file too large" in low


def _is_einval(text: str) -> bool:
    low = text.lower()
    return "einval" in low or "invalid argument" in low


def _retry_reason(stderr: str) -> str | None:
    low = stderr.lower()
    if "eagain" in low or "resource temporarily unavailable" in low:
        return "eagain"
    if "lock" in low and ("enoent" in low or "no such file" in low or "not found" in low):
        return "enoent"
    return None


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            return


def _read_some(pipe, buf: bytearray, *, limit: int | None) -> bool:
    try:
        chunk = pipe.read(65536)
    except BlockingIOError:
        return False
    if not chunk:
        return False
    buf.extend(chunk)
    # Stderr is kept only for error matching. Stdout is kept whole: `snapshots --json` is parsed.
    if limit is not None and len(buf) > limit:
        del buf[:-limit]
    return True


def _stream(argv: list[str], env: dict[str, str], timeout: float) -> CommandResult:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    assert proc.stdout is not None and proc.stderr is not None
    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)
    out = bytearray()
    err = bytearray()
    efbig = False
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        got_out = _read_some(proc.stdout, out, limit=None)
        got_err = _read_some(proc.stderr, err, limit=65536)
        if _is_efbig(err.decode("utf-8", "replace")):
            efbig = True
            _kill(proc)
            break
        if proc.poll() is not None:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill(proc)
            break
        if not got_out and not got_err:
            time.sleep(0.02)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.wait(timeout=5)
    for _ in range(50):
        if not _read_some(proc.stdout, out, limit=None) and not _read_some(proc.stderr, err, limit=65536):
            break
    stderr = err.decode("utf-8", "replace")
    stdout = out.decode("utf-8", "replace")
    return CommandResult(returncode=proc.returncode if proc.returncode is not None else -9,
                         stdout=stdout, stderr=stderr, efbig=efbig or _is_efbig(stderr),
                         locked=proc.returncode == 11, timed_out=timed_out)


def _execute(argv: list[str], env: dict[str, str], timeout: float, runner: Runner | None,
             password: str) -> CommandResult:
    if any(password in arg for arg in argv):
        raise BackupFailed("password_in_argv")
    if "--password-command" in argv or "--password-file" in argv or "-p" in argv:
        raise BackupFailed("password_in_argv")
    if runner is not None:
        result = runner(list(argv), dict(env), timeout)
        result.efbig = result.efbig or _is_efbig(result.stderr)
        result.locked = result.locked or result.returncode == 11
        return result
    return _stream(argv, env, timeout)


def call_restic(paths: Paths, repo: Path, command: list[str], *, password: str, timeout: float,
                runner: Runner | None = None, extra_global: list[str] | None = None) -> CommandResult:
    """Run one restic command. Retry once on EAGAIN or a lock-listing ENOENT. Never retry EFBIG or lock-busy."""
    cache = _cache_dir(paths, repo)
    binary = Path("restic") if runner is not None else restic_binary()
    argv = _build_argv(binary, repo, cache, command, extra_global)
    env = _command_env(password, cache)
    last: CommandResult | None = None
    for attempt in (1, 2):
        last = _execute(argv, env, timeout, runner, password)
        if last.efbig or _is_einval(last.stderr) or last.locked or last.timed_out or last.returncode == 0:
            return last
        if last.returncode == 12:
            return last
        if attempt == 1 and _retry_reason(last.stderr):
            continue
        return last
    assert last is not None
    return last


def _tail(text: str, password: str) -> str:
    cleaned = text.replace(password, "[REDACTED]") if password else text
    return redact(cleaned)[-300:]


def _emit(conn: sqlite3.Connection, kind: str, **data: Any) -> None:
    if "detail" in data and isinstance(data["detail"], str):
        data["detail"] = redact(data["detail"])[:300]
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) VALUES (?,?,?,?,?,?,?)",
        (db.utcnow(), None, None, kind, "backup", None, payload))


def _fail(conn: sqlite3.Connection, reason: str, **data: Any) -> None:
    _emit(conn, "backup_failed", reason=reason, **data)
    raise BackupFailed(reason, **{k: v for k, v in data.items() if k != "detail"})


def _policy(paths: Paths) -> dict:
    return load_policy(paths.config_dir)


def _timeout(policy: dict) -> float:
    return float(policy["backup_timeout_seconds"])


def _require_repo(policy: dict) -> Path:
    repo = repo_path(policy)
    if repo is None:
        raise BackupFailed("repo_unset")
    return repo


def repo_initialized(repo: Path) -> bool:
    config = repo / "config"
    return config.is_file() and not config.is_symlink()


def init_repo(paths: Paths, *, runner: Runner | None = None) -> dict[str, Any]:
    policy = _policy(paths)
    repo = _require_repo(policy)
    password = require_password()
    if repo_initialized(repo):
        return {"ok": True, "initialized": True, "already": True, "repo": str(repo)}
    repo.parent.mkdir(parents=True, exist_ok=True)
    result = call_restic(paths, repo, ["init"], password=password, timeout=_timeout(policy), runner=runner)
    if result.efbig:
        raise BackupFailed("efbig")
    if _is_einval(result.stderr):
        raise BackupFailed("einval")
    if result.returncode == 0 or "config file already exists" in result.stderr.lower():
        return {"ok": True, "initialized": True, "already": result.returncode != 0, "repo": str(repo)}
    if result.returncode == 12:
        raise BackupFailed("password_rejected")
    raise BackupFailed("restic", exit_code=result.returncode, detail=_tail(result.stderr, password))


def run_backup(conn: sqlite3.Connection, paths: Paths, *, force: bool = False, now: float | None = None,
               runner: Runner | None = None) -> dict[str, Any]:
    policy = _policy(paths)
    moment = time.time() if now is None else now
    status = load_status(paths)
    gen_now = current_generation(conn)
    if not force and not backup_due(last_at=status.get("last_backup_unix"), last_gen=status.get("last_generation"),
                                    gen=gen_now, now=moment,
                                    min_interval=policy["backup_min_interval_seconds"],
                                    max_interval=policy["backup_max_interval_seconds"]):
        return {"ok": True, "skipped": True, "reason": "schedule"}
    if not force and _efbig_backoff(status, moment, policy):
        return {"ok": True, "skipped": True, "reason": "efbig"}
    repo = _require_repo(policy)
    password = require_password()
    timeout = _timeout(policy)
    staging = prepare_staging(paths)
    try:
        meta = stage_snapshot(conn, paths, staging)
        command = ["backup", "--host", HOST, "--tag", "omg", "--tag", f"gen:{meta['generation']}",
                   "--tag", f"schema:{meta['schema']}", str(meta["tree"])]
        result = call_restic(paths, repo, command, password=password, timeout=timeout, runner=runner)
        if result.locked:
            result = _retry_after_own_stale_lock(conn, paths, repo, command, password=password, timeout=timeout,
                                                 runner=runner)
        if result.efbig:
            update_status(paths, last_attempt_unix=moment, last_failure_reason="efbig")
            _fail(conn, "efbig")
        if result.locked:
            return {"ok": True, "skipped": True, "reason": "locked"}
        if result.timed_out:
            update_status(paths, last_attempt_unix=moment, last_failure_reason="timeout")
            _fail(conn, "timeout")
        if result.returncode == 12:
            _fail(conn, "password_rejected")
        if result.returncode != 0:
            update_status(paths, last_attempt_unix=moment, last_failure_reason="restic")
            _fail(conn, "restic", exit_code=result.returncode, detail=_tail(result.stderr, password))
        _emit(conn, "backup_ok", generation=meta["generation"], schema=meta["schema"])
        # Record the id after backup_ok so that event is not itself a reason to back up again.
        update_status(paths, last_backup_unix=moment, last_attempt_unix=moment,
                      last_generation=current_generation(conn), pack_size=PACK_MIB,
                      last_schema=meta["schema"], last_failure_reason=None)
        return {"ok": True, "skipped": False, "generation": meta["generation"], "schema": meta["schema"],
                "tags": ["omg", f"gen:{meta['generation']}", f"schema:{meta['schema']}"], "host": HOST}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def prune_repo(conn: sqlite3.Connection, paths: Paths, *, runner: Runner | None = None,
               now: float | None = None) -> dict[str, Any]:
    policy = _policy(paths)
    repo = _require_repo(policy)
    password = require_password()
    moment = time.time() if now is None else now
    # Group by host only. Tags include gen: and schema:, which change every snapshot, so grouping
    # by tags would keep every generation. The staging path is stable, and host grouping still
    # applies if that path ever changes.
    command = ["forget", "--group-by", "host", "--keep-hourly", str(policy["backup_keep_hourly"]),
               "--keep-daily", str(policy["backup_keep_daily"]),
               "--keep-weekly", str(policy["backup_keep_weekly"]), "--prune"]
    result = call_restic(paths, repo, command, password=password, timeout=_timeout(policy), runner=runner)
    if result.locked:
        result = _retry_after_own_stale_lock(conn, paths, repo, command, password=password,
                                             timeout=_timeout(policy), runner=runner)
    if result.efbig:
        update_status(paths, last_prune_attempt_unix=moment, last_prune_failure_reason="efbig")
        _fail(conn, "efbig")
    if result.locked:
        return {"ok": True, "skipped": True, "reason": "locked"}
    if result.returncode != 0:
        update_status(paths, last_prune_attempt_unix=moment, last_prune_failure_reason="prune")
        _fail(conn, "prune", exit_code=result.returncode, detail=_tail(result.stderr, password))
    update_status(paths, last_prune_unix=moment, pack_size=PACK_MIB, last_prune_failure_reason=None)
    return {"ok": True, "pruned": True, "keep_hourly": policy["backup_keep_hourly"],
            "keep_daily": policy["backup_keep_daily"], "keep_weekly": policy["backup_keep_weekly"]}


def check_repo(conn: sqlite3.Connection, paths: Paths, *, read_data: bool = False, runner: Runner | None = None,
               now: float | None = None) -> dict[str, Any]:
    policy = _policy(paths)
    repo = _require_repo(policy)
    password = require_password()
    moment = time.time() if now is None else now
    command = ["check"]
    if read_data:
        command.extend(["--read-data-subset", "5%"])
    result = call_restic(paths, repo, command, password=password, timeout=_timeout(policy), runner=runner)
    if result.locked:
        result = _retry_after_own_stale_lock(conn, paths, repo, command, password=password,
                                             timeout=_timeout(policy), runner=runner)
    if result.locked:
        return {"ok": True, "skipped": True, "reason": "locked"}
    if result.returncode != 0:
        _fail(conn, "check", exit_code=result.returncode, detail=_tail(result.stderr, password))
    fields: dict[str, Any] = {"last_check_unix": moment}
    if read_data:
        fields["last_read_data_unix"] = moment
    update_status(paths, **fields)
    return {"ok": True, "checked": True, "read_data_subset": "5%" if read_data else None}


def scheduled(conn: sqlite3.Connection, paths: Paths, *, now: float | None = None,
              runner: Runner | None = None) -> dict[str, Any]:
    """Watchdog entry. The backup flag gates the step. A locked repository is skipped until the next tick."""
    policy = _policy(paths)
    if policy["features"].get("backup") != features.ON:
        return {"skipped": True, "reason": "backup off"}
    repo = repo_path(policy)
    if repo is None or not repo_initialized(repo):
        return {"skipped": True, "reason": "not_initialized"}
    moment = time.time() if now is None else now
    backup = run_backup(conn, paths, force=False, now=moment, runner=runner)
    if backup.get("reason") == "locked":
        return {"backup": backup, "skipped": True, "reason": "locked"}
    out: dict[str, Any] = {"backup": backup}
    status = load_status(paths)
    prune_blocked = _efbig_backoff(status, moment, policy, reason_key="last_prune_failure_reason",
                                   attempt_key="last_prune_attempt_unix", success_key="last_prune_unix",
                                   max_interval_key="backup_prune_interval_seconds")
    if interval_due(status.get("last_prune_unix"), moment, policy["backup_prune_interval_seconds"]) and not prune_blocked:
        out["prune"] = prune_repo(conn, paths, runner=runner, now=moment)
        if out["prune"].get("reason") == "locked":
            return out
        status = load_status(paths)
    read_due = interval_due(status.get("last_read_data_unix"), moment, policy["backup_read_data_interval_seconds"])
    check_due = interval_due(status.get("last_check_unix"), moment, policy["backup_check_interval_seconds"])
    if read_due or check_due:
        out["check"] = check_repo(conn, paths, read_data=read_due, runner=runner, now=moment)
    return out


# ---------------------------------------------------------------- locks
def parse_restic_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        digits = []
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits.append(char)
            else:
                rest = tail[index:]
                break
        text = head + "." + ("".join(digits) + "000000")[:6] + rest
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def lock_age_seconds(lock: dict[str, Any], now: float) -> float:
    return now - parse_restic_time(str(lock["time"])).timestamp()


def list_locks(paths: Paths, repo: Path, *, password: str, timeout: float,
               runner: Runner | None = None) -> list[dict[str, Any]]:
    # --no-lock so an exclusive lock can be classified instead of failing the read with exit 11.
    no_lock = ["--no-lock"]
    listed = call_restic(paths, repo, ["list", "locks"], password=password, timeout=timeout, runner=runner,
                         extra_global=no_lock)
    if listed.locked:
        raise BackupFailed("locked")
    if listed.returncode != 0:
        raise BackupFailed("restic", exit_code=listed.returncode, detail=_tail(listed.stderr, password))
    locks = []
    for lock_id in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
        cat = call_restic(paths, repo, ["cat", "lock", lock_id], password=password, timeout=timeout, runner=runner,
                          extra_global=no_lock)
        if cat.locked or cat.returncode == 11:
            raise BackupFailed("locked")
        if cat.returncode != 0:
            raise BackupFailed("restic", exit_code=cat.returncode, detail=_tail(cat.stderr, password))
        try:
            data = json.loads(cat.stdout)
        except json.JSONDecodeError as exc:
            raise BackupFailed("restic", detail="lock was not json") from exc
        if not isinstance(data, dict):
            raise BackupFailed("restic", detail="lock was not json")
        data["id"] = lock_id
        locks.append(data)
    return locks


def classify_locks(locks: list[dict[str, Any]], *, now: float, hostname: str,
                   pid_alive_fn: Callable[[int], bool]) -> str:
    """clear, stale, or wait. ``wait`` means a live lock or a young lock from another machine: do not unlock."""
    if not locks:
        return "clear"
    for lock in locks:
        machine = str(lock.get("hostname") or "")
        try:
            age = lock_age_seconds(lock, now)
        except (KeyError, TypeError, ValueError):
            return "wait"
        if machine != hostname and age < LOCK_YOUNG_SECONDS:
            return "wait"
        if machine == hostname:
            try:
                pid = int(lock.get("pid") or 0)
            except (TypeError, ValueError):
                return "wait"
            if pid_alive_fn(pid):
                return "wait"
    return "stale"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def unlock_stale(paths: Paths, repo: Path, *, password: str, timeout: float,
                 runner: Runner | None = None) -> CommandResult:
    """Plain ``restic unlock``. There is no ``--remove-all`` path."""
    return call_restic(paths, repo, ["unlock"], password=password, timeout=timeout, runner=runner)


def release_own_stale_lock(paths: Paths, repo: Path, *, password: str, timeout: float,
                           runner: Runner | None = None) -> bool:
    """Remove a lock left by a dead restic on this machine. A foreign or live lock is left alone."""
    try:
        locks = list_locks(paths, repo, password=password, timeout=timeout, runner=runner)
    except BackupFailed:
        return False
    if not locks:
        return False
    host = socket.gethostname()
    for lock in locks:
        if str(lock.get("hostname") or "") != host:
            return False
        try:
            pid = int(lock.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid_alive(pid):
            return False
    unlocked = unlock_stale(paths, repo, password=password, timeout=timeout, runner=runner)
    return unlocked.returncode == 0


def _retry_after_own_stale_lock(conn: sqlite3.Connection, paths: Paths, repo: Path, command: list[str], *,
                                password: str, timeout: float, runner: Runner | None) -> CommandResult:
    if release_own_stale_lock(paths, repo, password=password, timeout=timeout, runner=runner):
        retried = call_restic(paths, repo, command, password=password, timeout=timeout, runner=runner)
        if not retried.locked:
            return retried
    _emit(conn, "backup_locked", reason="locked")
    return CommandResult(returncode=11, locked=True)


# ---------------------------------------------------------------- doctor
def _walk_repo(repo: Path) -> tuple[int, list[tuple[str, int]]]:
    total = 0
    large: list[tuple[str, int]] = []
    if not repo.exists():
        return 0, large
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            if size >= DOCTOR_FILE_LIMIT:
                large.append((path.relative_to(repo).as_posix(), size))
    return total, large


def _failure_view(paths: Paths) -> tuple[int, bool]:
    if not paths.db_path.exists():
        return 0, False
    conn = db.connect(paths.db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT kind, data_json FROM events WHERE kind IN ('backup_failed', 'backup_ok') ORDER BY id"
            ).fetchall()
        except sqlite3.Error:
            return 0, False
    finally:
        conn.close()
    consecutive = 0
    efbig = False
    for row in reversed(rows):
        if row["kind"] == "backup_ok":
            break
        if row["kind"] != "backup_failed":
            continue
        consecutive += 1
        try:
            data = json.loads(row["data_json"] or "{}")
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("reason") == "efbig":
            efbig = True
    return consecutive, efbig


def _check(name: str, ok: bool, detail: str, status: str | None = None) -> dict[str, Any]:
    if status is None:
        status = "PASS" if ok else "FAIL"
    return {"check": name, "ok": bool(ok), "status": status, "detail": detail}


def _restic_version(binary: Path) -> str:
    try:
        cp = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=15, check=False,
                            env=scrubbed_environ())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (cp.stdout or "") + (cp.stderr or "")


def doctor_report(paths: Paths | None = None, *, now: float | None = None,
                  runner: Runner | None = None) -> list[dict[str, Any]]:
    """Checks for ``omg doctor``. WARN and SKIP keep ``ok`` true so they do not fail the command."""
    paths = paths or get_paths()
    try:
        policy = _policy(paths)
    except OmgError as exc:
        return [_check("backup_repo", False, exc.message)]
    password = "set" if password_is_set() else "unset"
    if policy["features"].get("backup") != features.ON:
        return [
            _check("backup_restic", True, "backup off", "SKIP"),
            _check("backup_repo", True, "backup off", "SKIP"),
            _check("backup_files", True, "backup off", "SKIP"),
            _check("backup_failures", True, "backup off", "SKIP"),
            _check("backup_pack_size", True, "backup off", "SKIP"),
            _check("backup_locks", True, "backup off", "SKIP"),
            _check("backup_store_size", True, "backup off", "SKIP"),
            _check("backup_password", True, password, "PASS"),
        ]
    checks = []
    try:
        binary = restic_binary()
    except BackupFailed:
        binary = None
    if binary is None:
        checks.append(_check("backup_restic", False, "restic binary not found"))
    else:
        version = _restic_version(binary)
        if "restic 0.19.1" in version:
            checks.append(_check("backup_restic", True, "restic 0.19.1"))
        else:
            shown = " ".join(version.split())[:120] or "unknown"
            checks.append(_check("backup_restic", True, f"version is not 0.19.1 ({shown})", "WARN"))
    repo = repo_path(policy)
    if repo is None:
        checks.append(_check("backup_repo", False, "backup_repo is empty"))
        checks.append(_check("backup_files", True, "repo not configured", "SKIP"))
        checks.append(_check("backup_store_size", True, "repo not configured", "SKIP"))
        checks.append(_check("backup_locks", True, "repo not configured", "SKIP"))
    elif not repo.exists():
        checks.append(_check("backup_repo", False, "backup_repo does not exist"))
        checks.append(_check("backup_files", True, "repo missing", "SKIP"))
        checks.append(_check("backup_store_size", True, "repo missing", "SKIP"))
        checks.append(_check("backup_locks", True, "repo missing", "SKIP"))
    else:
        total, large = _walk_repo(repo)
        if large:
            shown = ", ".join(f"{name}={size}" for name, size in large[:5])
            checks.append(_check("backup_repo", True, str(repo)))
            checks.append(_check("backup_files", False, shown))
        else:
            checks.append(_check("backup_repo", True, str(repo)))
            checks.append(_check("backup_files", True, "no file >= 15 MiB"))
        checks.append(_check("backup_store_size", True, f"bytes={total}"))
        checks.append(_lock_check(paths, repo, policy, now=now, runner=runner))
    consecutive, efbig = _failure_view(paths)
    if efbig or consecutive >= 3:
        reason = "recent efbig" if efbig else f"{consecutive} consecutive failures"
        checks.append(_check("backup_failures", False, reason))
    else:
        checks.append(_check("backup_failures", True, f"consecutive={consecutive}"))
    pack = load_status(paths).get("pack_size")
    if pack == PACK_MIB:
        checks.append(_check("backup_pack_size", True, "pack size 4"))
    else:
        shown = "unset" if pack is None else str(pack)
        checks.append(_check("backup_pack_size", True, f"pack size is {shown}, expected 4", "WARN"))
    if password == "set":
        checks.append(_check("backup_password", True, "set"))
    else:
        checks.append(_check("backup_password", False, "unset"))
    return checks


def _lock_check(paths: Paths, repo: Path, policy: dict, *, now: float | None,
                runner: Runner | None) -> dict[str, Any]:
    if not password_is_set():
        return _check("backup_locks", True, "not read (password unset)")
    moment = time.time() if now is None else now
    try:
        locks = list_locks(paths, repo, password=require_password(), timeout=_timeout(policy), runner=runner)
    except BackupFailed as exc:
        return _check("backup_locks", True, f"not read ({exc.reason})", "WARN")
    machines = []
    oldest = 0
    for lock in locks:
        machines.append(str(lock.get("hostname") or "?"))
        try:
            age = max(0, int(lock_age_seconds(lock, moment)))
        except (KeyError, TypeError, ValueError):
            age = -1
        oldest = max(oldest, age)
    detail = f"count={len(locks)} oldest_age_seconds={oldest} machines={','.join(machines) or '-'}"
    if locks and (oldest < 0 or oldest >= LOCK_YOUNG_SECONDS):
        return _check("backup_locks", False, detail)
    return _check("backup_locks", True, detail)
