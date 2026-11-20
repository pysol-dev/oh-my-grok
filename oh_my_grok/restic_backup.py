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

import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import re
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
from .paths import Paths, get_paths, scrubbed_environ, tools_dir
from .redact import redact, register_secret

PACK_MIB = 4
HOST = "omg-box"
PASSWORD_COMMAND = "printenv OMG_RESTIC_PASSWORD"
PASSWORD_ENV = "OMG_RESTIC_PASSWORD"
# Measured store cap. One byte over fails with EFBIG, including a temp file that is then renamed.
STORE_FILE_CAP = 16 * 1024 * 1024
# Doctor fails a file before it can reach that cap.
DOCTOR_FILE_LIMIT = 15 * 1024 * 1024
LOCK_YOUNG_SECONDS = 30 * 60
MIN_PASSWORD_CHARS = 12
REDACT_PASSWORD_CHARS = 4
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
# Distinct from exit 12, which is a failed check or a newer snapshot schema.
EXIT_ARGV = 17

Runner = Callable[[list[str], dict[str, str], float], "CommandResult"]


class BackupFailed(OmgError):
    """A backup, prune, or check failed. ``reason`` is safe to log; it is never the password."""

    code = "backup_failed"

    def __init__(self, reason: str, **details):
        self.reason = reason
        self.exit_code = {
            "password_unset": EXIT_USAGE,
            "password_short": EXIT_USAGE,
            "password_rejected": EXIT_PASSWORD,
            "locked": EXIT_LOCKED,
            "repo_unset": EXIT_USAGE,
            "restic_missing": EXIT_USAGE,
            "password_in_argv": EXIT_ARGV,
        }.get(reason, 1)
        super().__init__(f"backup failed: {reason}", reason=reason, **details)


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    efbig: bool = False
    einval: bool = False
    locked: bool = False
    timed_out: bool = False


def password_is_set() -> bool:
    return bool(os.environ.get(PASSWORD_ENV))


def read_password() -> str:
    """The password that opens an existing repository. Length is not checked.

    Init and doctor reject a password shorter than 12 characters. Restore accepts any
    password restic accepts, including one chosen before that minimum existed.
    """
    password = os.environ.get(PASSWORD_ENV) or ""
    if not password:
        raise BackupFailed("password_unset")
    if len(password) >= REDACT_PASSWORD_CHARS:
        register_secret(password, min_length=REDACT_PASSWORD_CHARS)
    return password


def require_password() -> str:
    password = read_password()
    if len(password) < MIN_PASSWORD_CHARS:
        raise BackupFailed("password_short")
    return password


def _password_for(conn: sqlite3.Connection, paths: Paths, *, now: float | None = None) -> str:
    """Like ``require_password``, and record a failure the doctor can see.

    ``password_unset`` is recorded when the password state changes, then at most once an hour.
    A watchdog tick every minute must not append a new event each time.
    """
    try:
        return require_password()
    except BackupFailed as exc:
        if exc.reason == "password_unset":
            _emit_password_unset(conn, paths, time.time() if now is None else now)
        elif exc.reason == "password_short":
            _fail(conn, exc.reason)
        raise


def _emit_password_unset(conn: sqlite3.Connection, paths: Paths, now: float) -> None:
    status = load_status(paths)
    last = status.get("last_password_unset_unix")
    same = status.get("password_failure") == "password_unset"
    if (same and isinstance(last, (int, float)) and not isinstance(last, bool)
            and now - float(last) < 3600):
        return
    update_status(paths, last_password_unset_unix=now, password_failure="password_unset")
    _emit(conn, "backup_failed", reason="password_unset")


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
                   max_interval_key: str = "backup_max_interval_seconds",
                   honor_ceiling: bool = True) -> bool:
    """Block a retry until the minimum interval after EFBIG.

    Backup may still run when the long ceiling has elapsed. Prune must not: its ceiling is the
    same number as "prune is due", which would cancel the wait on every tick.
    """
    if status.get(reason_key) != "efbig":
        return False
    attempt = status.get(attempt_key)
    if not isinstance(attempt, (int, float)) or isinstance(attempt, bool):
        return False
    if now - float(attempt) >= float(policy["backup_min_interval_seconds"]):
        return False
    if not honor_ceiling:
        return True
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
    if root.is_symlink():
        raise IntegrityError_("staging path is a symlink")
    if root.exists():
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise IntegrityError_("staging path is not a directory")
        if info.st_uid != os.getuid():
            raise IntegrityError_("staging path is not owned by the current user")
        shutil.rmtree(root)
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _private_staging(root: Path) -> bool:
    """A leftover from this user: a real directory, mode 0700, not a symlink."""
    try:
        info = root.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    return info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


def reap_stale_staging(paths: Paths) -> bool:
    """Remove this home's leftover staging directory.

    A killed backup leaves a mode-0700 directory that contains a database copy and
    ``hooks.json``. A live backup, prune, check, or restore holds the operation lock, so
    this does not remove a directory that work is using. The lock file is under the omg
    home, never on the agent store.
    """
    directory = paths.state_dir / "backup"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(directory / "operation.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            root = staging_root(paths)
            if not _private_staging(root):
                return False
            shutil.rmtree(root)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def operation_lock(conn: sqlite3.Connection | None, paths: Paths, *, record: bool = True,
                   create: bool = True):
    """Exclusive lock for backup, prune, check, and restore. A busy lock skips.

    The lock file lives under the omg home, never on the agent store. ``conn`` may be
    None when the database cannot be opened; the busy lock is still reported to the caller.
    Restore passes ``record=False`` so a contended check does not append an event and
    change the stream that a later restore compares. ``--check`` passes ``create=False``
    so a missing lock file is not created and the home is not written.
    """
    directory = paths.state_dir / "backup"
    lock_path = directory / "operation.lock"
    if not create and not lock_path.exists():
        yield True
        return
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    else:
        fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        if record and conn is not None:
            _emit(conn, "backup_locked", reason="locked")
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


_cache_override: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "omg_restic_cache_override", default=None)


@contextlib.contextmanager
def external_cache(path: Path):
    """Use a cache outside the omg home. ``--check`` must not create files there."""
    token = _cache_override.set(path)
    try:
        yield
    finally:
        _cache_override.reset(token)


def _cache_dir(paths: Paths, repo: Path) -> Path:
    override = _cache_override.get()
    if override is not None:
        return override
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
        err_text = err.decode("utf-8", "replace")
        if _is_efbig(err_text) or _is_einval(err_text):
            efbig = _is_efbig(err_text)
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
                         einval=_is_einval(stderr), locked=proc.returncode == 11, timed_out=timed_out)


# Literals this code always puts on the restic command. A short password can be a substring of
# one of them (`a` is inside `--pack-size`) without the password having been placed on argv.
_FIXED_ARGV = frozenset({
    "--repo", "--pack-size", str(PACK_MIB), "--cache-dir", "--no-lock", "--retry-lock", "2m",
    "--host", HOST, "--tag", "omg", "--group-by", "host",
    "--keep-hourly", "--keep-daily", "--keep-weekly", "--prune",
    "--read-data-subset", "5%", "--target",
    "backup", "forget", "check", "init", "unlock", "snapshots", "restore", "latest",
    "version", "list", "locks", "cat", "lock",
})
_GENERATED_TAG = re.compile(r"(?:gen|schema):\d+\Z")


def _argv_discloses_password(password: str, argv: list[str], *, allowed: set[str] | None = None) -> bool:
    """True when the password would be passed as an argv token or embedded in a value we built.

    The repository path, cache directory, restore target, omg home, and restic binary are
    arguments this process chooses. A password that only occurs inside one of those paths is
    not disclosure. Fixed flags and generated ``gen:`` / ``schema:`` tags are not disclosure
    either. A password shorter than 4 characters is only a disclosure when it is an entire
    argument, because it would otherwise match ordinary flags.
    """
    if not password:
        return False
    own_paths = allowed or set()
    for arg in argv:
        if arg in _FIXED_ARGV or arg in own_paths or _GENERATED_TAG.fullmatch(arg):
            continue
        if arg == password:
            return True
        if len(password) >= REDACT_PASSWORD_CHARS and password in arg:
            return True
    return False


def _own_argv_paths(paths: Paths, repo: Path, cache: Path, command: list[str], binary: Path) -> set[str]:
    """Paths this code passes through, not values built from the password."""
    owned = {str(repo), str(cache), str(binary), str(paths.root)}
    for path in (repo, cache, binary, paths.root):
        try:
            owned.add(str(path.resolve()))
        except OSError:
            continue
    if "--target" in command:
        index = command.index("--target")
        if index + 1 < len(command):
            owned.add(command[index + 1])
    return owned


def _execute(argv: list[str], env: dict[str, str], timeout: float, runner: Runner | None,
             password: str, *, allowed: set[str] | None = None) -> CommandResult:
    if _argv_discloses_password(password, argv, allowed=allowed):
        raise BackupFailed("password_in_argv")
    if "--password-command" in argv or "--password-file" in argv or "-p" in argv:
        raise BackupFailed("password_in_argv")
    if runner is not None:
        result = runner(list(argv), dict(env), timeout)
        result.efbig = result.efbig or _is_efbig(result.stderr)
        result.einval = result.einval or _is_einval(result.stderr)
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
    allowed = _own_argv_paths(paths, repo, cache, command, binary)
    last: CommandResult | None = None
    for attempt in (1, 2):
        last = _execute(argv, env, timeout, runner, password, allowed=allowed)
        if last.efbig or last.einval or _is_einval(last.stderr) or last.locked or last.timed_out or last.returncode == 0:
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
    reap_stale_staging(paths)
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
    password = _password_for(conn, paths, now=moment)
    timeout = _timeout(policy)
    with operation_lock(conn, paths) as held:
        if not held:
            return {"ok": True, "skipped": True, "reason": "locked"}
        return _run_backup_locked(conn, paths, repo, password, timeout, moment, runner)


def _require_staged_database(meta: dict[str, Any]) -> None:
    database = Path(meta["tree"]) / "state" / meta["db_name"]
    if not database.is_file() or database.is_symlink():
        raise IntegrityError_("staged database is missing")
    health, _events = inspect_database(database)
    if health != "ok":
        raise IntegrityError_("staged database failed integrity_check")


def _run_backup_locked(conn, paths, repo, password, timeout, moment, runner) -> dict[str, Any]:
    staging = prepare_staging(paths)
    try:
        meta = stage_snapshot(conn, paths, staging)
        try:
            _require_staged_database(meta)
        except IntegrityError_:
            _emit(conn, "backup_failed", reason="integrity")
            raise
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
                      last_schema=meta["schema"], last_failure_reason=None,
                      password_failure=None, last_password_unset_unix=None)
        return {"ok": True, "skipped": False, "generation": meta["generation"], "schema": meta["schema"],
                "tags": ["omg", f"gen:{meta['generation']}", f"schema:{meta['schema']}"], "host": HOST}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def prune_repo(conn: sqlite3.Connection, paths: Paths, *, runner: Runner | None = None,
               now: float | None = None) -> dict[str, Any]:
    policy = _policy(paths)
    repo = _require_repo(policy)
    moment = time.time() if now is None else now
    password = _password_for(conn, paths, now=moment)
    with operation_lock(conn, paths) as held:
        if not held:
            return {"ok": True, "skipped": True, "reason": "locked"}
        return _prune_locked(conn, paths, policy, repo, password, moment, runner)


def _prune_locked(conn, paths, policy, repo, password, moment, runner) -> dict[str, Any]:
    # Group by host only. Tags include gen: and schema:, which change every snapshot, so grouping
    # by tags would keep every generation. --host and --tag keep foreign snapshots out of this policy.
    command = ["forget", "--group-by", "host", "--host", HOST, "--tag", "omg",
               "--keep-hourly", str(policy["backup_keep_hourly"]),
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
    moment = time.time() if now is None else now
    password = _password_for(conn, paths, now=moment)
    with operation_lock(conn, paths) as held:
        if not held:
            return {"ok": True, "skipped": True, "reason": "locked"}
        return _check_locked(conn, paths, repo, password, moment, read_data, runner)


def _check_locked(conn, paths, repo, password, moment, read_data, runner) -> dict[str, Any]:
    policy = _policy(paths)
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
                                   max_interval_key="backup_prune_interval_seconds", honor_ceiling=False)
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


def _failure_view(paths: Paths) -> tuple[int, bool, bool]:
    if not paths.db_path.exists():
        return 0, False, False
    conn = db.connect(paths.db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT kind, data_json FROM events WHERE kind IN ('backup_failed', 'backup_ok') ORDER BY id"
            ).fetchall()
        except sqlite3.Error:
            return 0, False, False
    finally:
        conn.close()
    consecutive = 0
    efbig = False
    recent_failure = bool(rows) and rows[-1]["kind"] == "backup_failed"
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
    return consecutive, efbig, recent_failure


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
    moment = time.time() if now is None else now
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
            _check("backup_age", True, "backup off", "SKIP"),
        ]
    reap_stale_staging(paths)
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
    consecutive, efbig, recent_failure = _failure_view(paths)
    if recent_failure or efbig or consecutive >= 3:
        reason = "recent efbig" if efbig else "recent backup failure"
        checks.append(_check("backup_failures", False, reason))
    else:
        checks.append(_check("backup_failures", True, f"consecutive={consecutive}"))
    pack = load_status(paths).get("pack_size")
    if pack == PACK_MIB:
        checks.append(_check("backup_pack_size", True, "pack size 4"))
    else:
        shown = "unset" if pack is None else str(pack)
        checks.append(_check("backup_pack_size", True, f"pack size is {shown}, expected 4", "WARN"))
    checks.append(_password_check(paths, policy, moment))
    checks.append(_backup_age_check(paths, policy, moment))
    return checks


def _read_heartbeat(paths: Paths) -> dict | None:
    try:
        beat = json.loads((paths.state_dir / "watchdog" / "heartbeat.json").read_text())
    except (OSError, ValueError):
        return None
    return beat if isinstance(beat, dict) else None


def _heartbeat_stale(beat: dict | None, policy: dict, now: float) -> bool:
    """Same limit the watchdog uses: interval times watchdog_stale_intervals."""
    if not isinstance(beat, dict):
        return True
    unix = beat.get("unix")
    if isinstance(unix, bool) or not isinstance(unix, (int, float)):
        return True
    interval = beat.get("interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or float(interval) <= 0:
        interval = policy["watchdog_interval_seconds"]
    limit = float(interval) * float(policy["watchdog_stale_intervals"])
    return now - float(unix) > limit


def _password_check(paths: Paths, policy: dict, now: float) -> dict[str, Any]:
    """With backup on, trust a fresh watchdog heartbeat, not the shell that happens to run doctor."""
    raw = os.environ.get(PASSWORD_ENV) or ""
    if raw and len(raw) < MIN_PASSWORD_CHARS:
        return _check("backup_password", False, "shorter than 12")
    beat = _read_heartbeat(paths)
    if _heartbeat_stale(beat, policy, now):
        return _check("backup_password", False, "watchdog heartbeat is stale")
    reported = beat.get("restic_password") if beat else None
    if reported == "unset":
        return _check("backup_password", False, "watchdog password unset")
    if reported != "set":
        return _check("backup_password", False, "watchdog has not reported the password")
    return _check("backup_password", True, "set")


def _backup_age_check(paths: Paths, policy: dict, now: float) -> dict[str, Any]:
    limit = int(policy["backup_max_age_seconds"])
    last = load_status(paths).get("last_backup_unix")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        return _check("backup_age", False, f"no successful backup limit_seconds={limit}")
    age = int(now - float(last))
    detail = f"age_seconds={age} limit_seconds={limit}"
    if age > limit:
        return _check("backup_age", False, detail)
    return _check("backup_age", True, detail)


def _lock_check(paths: Paths, repo: Path, policy: dict, *, now: float | None,
                runner: Runner | None) -> dict[str, Any]:
    raw = os.environ.get(PASSWORD_ENV) or ""
    if len(raw) < MIN_PASSWORD_CHARS:
        return _check("backup_locks", True, "not read (password unset)", "WARN")
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
