"""Restore omg state from the restic repository.

``scripts/recover.sh`` is the operator wrapper. It bootstraps, sets OMG_DISABLE_TRANSPORT, and
then runs this module. Lock handling:

1. Read locks before changing anything.
2. A lock from another machine younger than 30 minutes, or a lock whose pid is still alive on
   this machine, waits with ``--retry-lock 2m`` and then exits 16. It is not unlocked.
3. Any other lock is stale. ``restic unlock`` removes it. ``--remove-all`` is never passed.
4. After a stale lock is removed, ``restic check`` must succeed before a snapshot is read.

Restore decisions use the event stream. A backup that extends the local stream is applied. A
local stream that is already ahead is kept. Diverged streams exit 11 and change nothing. A
snapshot schema newer than this code is refused. An older schema is migrated forward after the
files are in place.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import db
from .db import LATEST_VERSION
from .errors import IntegrityError_, OmgError
from .paths import Paths, get_paths
from .redact import redact, register_secret
from .restic_backup import (
    EXIT_CHECK_BEHIND,
    EXIT_DIVERGED,
    EXIT_LOCK,
    EXIT_NO_SNAPSHOT,
    EXIT_OK,
    EXIT_PASSWORD,
    EXIT_USAGE,
    EXIT_VERIFY,
    HOST,
    BackupFailed,
    Runner,
    _policy,
    _timeout,
    call_restic,
    classify_locks,
    find_snapshot_root,
    inspect_database,
    list_locks,
    password_is_set,
    pid_alive,
    relate_streams,
    repo_path,
    require_password,
    schema_version,
    unlock_stale,
)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in dirnames:
            (dest / (current / name).relative_to(src)).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            path = current / name
            rel = path.relative_to(src)
            if path.is_symlink():
                raise IntegrityError_(f"refusing to restore symlink {rel.as_posix()}")
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


def _write_private(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    partial = dest.with_name(dest.name + ".partial")
    try:
        partial.write_bytes(data)
        os.chmod(partial, 0o600)
        os.replace(partial, dest)
        os.chmod(dest, 0o600)
    finally:
        del data


def decide(local_health: str, local_events: list[dict[str, Any]] | None,
           backup_events: list[dict[str, Any]], snapshot_schema: int) -> str:
    """Return restore, keep, diverged, or newer_schema. Nothing is written here."""
    if snapshot_schema > LATEST_VERSION:
        return "newer_schema"
    if local_health != "ok" or local_events is None:
        return "restore"
    relation = relate_streams(local_events, backup_events)
    if relation == "diverged":
        return "diverged"
    if relation in ("equal", "local_newer"):
        return "keep"
    if relation == "local_prefix":
        return "restore"
    return "restore"


def apply_snapshot(tree: Path, paths: Paths) -> None:
    """Replace the database, jobs, config files present in the snapshot, and hooks. Then migrate."""
    database = _database_file(tree)
    health, _events = inspect_database(database)
    if health != "ok":
        raise IntegrityError_("snapshot failed integrity_check")
    paths.ensure()
    target = paths.db_path
    _preserve_local_database(paths, target)
    db.restore(database, target)
    _copy_tree(tree / "jobs", paths.jobs_dir)
    config_src = tree / "config"
    if config_src.is_dir() and not config_src.is_symlink():
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(config_src, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
            for name in filenames:
                path = current / name
                if path.is_symlink():
                    raise IntegrityError_("refusing to restore symlink config")
                rel = path.relative_to(config_src)
                _write_private(paths.config_dir / rel, path.read_bytes())
    hooks_src = tree / "state" / "secrets" / "hooks.json"
    if hooks_src.is_symlink():
        raise IntegrityError_("snapshot hooks.json is a symlink")
    if hooks_src.is_file():
        data = hooks_src.read_bytes()
        paths.secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.secrets_dir, 0o700)
        _write_private(paths.hooks_path, data)
        del data
    conn = db.connect(target)
    try:
        db.migrate(conn)
    finally:
        conn.close()


def _preserve_local_database(paths: Paths, target: Path) -> None:
    """Keep a copy of a readable database. A corrupt file is renamed aside, with its WAL siblings."""
    wal = [Path(str(target) + suffix) for suffix in ("-wal", "-shm")]
    present = target.exists() or any(path.exists() or path.is_symlink() for path in wal)
    if not present:
        return
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    health = "missing"
    if target.exists() and not target.is_symlink() and _sqlite_header(target):
        health, _events = inspect_database(target)
    if health == "ok":
        try:
            held = db.connect(target)
            try:
                db.backup(held, paths.backups_dir / f"pre-recover-{stamp}.sqlite3")
            finally:
                held.close()
            _unlink_db_sidecars(target)
            return
        except (OSError, sqlite3.Error):
            pass
    aside = paths.backups_dir / f"pre-recover-corrupt-{stamp}.sqlite3"
    if target.exists() or target.is_symlink():
        os.replace(target, aside)
    for extra in wal:
        if extra.exists() or extra.is_symlink():
            os.replace(extra, Path(str(aside) + extra.name[len(target.name):]))


def _sqlite_header(path: Path) -> bool:
    try:
        return path.read_bytes()[:16] == b"SQLite format 3\x00"
    except OSError:
        return False


def _unlink_db_sidecars(target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        extra = Path(str(target) + suffix)
        if extra.exists() or extra.is_symlink():
            extra.unlink()


def _database_file(tree: Path) -> Path:
    found = [path for path in (tree / "state").glob("*.sqlite3")
             if path.is_file() and not path.is_symlink() and path.name in ("omg.sqlite3", "omo.sqlite3")]
    if len(found) != 1:
        raise IntegrityError_("snapshot must contain exactly one database")
    return found[0]


def handle_locks(paths: Paths, repo: Path, *, password: str, timeout: float, check: bool,
                 now: float | None = None, hostname: str | None = None,
                 pid_alive_fn=None, runner: Runner | None = None) -> int | None:
    """Return an exit code when recovery must stop, or None when it may read the snapshot.

    ``check`` does not remove a lock and does not run ``restic check`` after one. A real restore
    removes only a stale lock, with plain ``unlock``, and then checks the repository.
    """
    moment = time.time() if now is None else now
    host = hostname if hostname is not None else socket.gethostname()
    alive = pid_alive if pid_alive_fn is None else pid_alive_fn
    try:
        locks = list_locks(paths, repo, password=password, timeout=timeout, runner=runner)
    except BackupFailed as exc:
        if exc.reason == "locked":
            return _wait_for_lock(paths, repo, password=password, timeout=timeout, runner=runner)
        if exc.exit_code == EXIT_PASSWORD or exc.reason == "password_rejected":
            print("restic password was rejected", file=sys.stderr)
            return EXIT_PASSWORD
        print(redact(exc.message), file=sys.stderr)
        return EXIT_VERIFY
    kind = classify_locks(locks, now=moment, hostname=host, pid_alive_fn=alive)
    if kind == "clear":
        return None
    if kind == "wait":
        if check:
            print("repository lock is held", file=sys.stderr)
            return EXIT_LOCK
        return _wait_for_lock(paths, repo, password=password, timeout=timeout, now=moment,
                              hostname=host, pid_alive_fn=alive, runner=runner)
    if check:
        print("stale repository lock; --check does not remove it", file=sys.stderr)
        return EXIT_LOCK
    unlocked = unlock_stale(paths, repo, password=password, timeout=timeout, runner=runner)
    if unlocked.returncode != 0:
        print("restic unlock failed", file=sys.stderr)
        return EXIT_VERIFY
    checked = call_restic(paths, repo, ["check"], password=password, timeout=timeout, runner=runner)
    if checked.returncode != 0:
        print("restic check failed after removing a stale lock", file=sys.stderr)
        return EXIT_VERIFY
    return None


def _wait_for_lock(paths, repo, *, password, timeout, runner, now=None, hostname=None,
                  pid_alive_fn=None) -> int | None:
    """Wait, then refuse to restore while a young foreign lock is still present."""
    waiting = max(timeout, 150.0)
    call_restic(paths, repo, ["snapshots", "--json"], password=password, timeout=waiting,
                runner=runner, extra_global=["--retry-lock", "2m"])
    moment = time.time() if now is None else now
    host = hostname if hostname is not None else socket.gethostname()
    alive = pid_alive if pid_alive_fn is None else pid_alive_fn
    try:
        locks = list_locks(paths, repo, password=password, timeout=timeout, runner=runner)
    except BackupFailed:
        print("repository lock is still held after --retry-lock 2m", file=sys.stderr)
        return EXIT_LOCK
    if classify_locks(locks, now=moment, hostname=host, pid_alive_fn=alive) == "wait":
        print("repository lock is still held after --retry-lock 2m", file=sys.stderr)
        return EXIT_LOCK
    return None


def recover_from_tree(paths: Paths, tree: Path, *, check: bool) -> int:
    """Decide from an already extracted snapshot tree. Writes only when the decision is restore."""
    try:
        database = _database_file(tree)
        version = schema_version(database)
        remote_health, remote_events = inspect_database(database)
    except (IntegrityError_, OmgError, sqlite3.Error):
        print("snapshot database could not be read", file=sys.stderr)
        return EXIT_VERIFY
    if remote_health != "ok" or remote_events is None:
        print("snapshot database failed integrity_check", file=sys.stderr)
        return EXIT_VERIFY
    local_health, local_events = inspect_database(paths.db_path)
    try:
        action = decide(local_health, local_events, remote_events, version)
    except (IntegrityError_, OmgError):
        print("snapshot database could not be read", file=sys.stderr)
        return EXIT_VERIFY
    if action == "newer_schema":
        print(f"refusing snapshot schema {version}; this code supports up to {LATEST_VERSION}", file=sys.stderr)
        return EXIT_VERIFY
    if action == "diverged":
        _emit({"action": "diverged", "relation": "diverged", "schema": version})
        return EXIT_DIVERGED
    if action == "keep":
        relation = "equal" if local_events == remote_events else "local_newer"
        _emit({"action": "noop", "relation": relation, "schema": version})
        return EXIT_OK
    if check:
        _emit({"action": "would_restore", "relation": "restore", "schema": version})
        return EXIT_CHECK_BEHIND
    try:
        apply_snapshot(tree, paths)
    except (IntegrityError_, OmgError, sqlite3.Error) as exc:
        print(redact(str(exc))[:300], file=sys.stderr)
        return EXIT_VERIFY
    _emit({"action": "restored", "relation": "restore", "schema": version})
    return EXIT_OK


def run_recover(*, check: bool = False, runner: Runner | None = None, now: float | None = None,
                hostname: str | None = None, pid_alive_fn=None) -> int:
    os.environ["OMG_DISABLE_TRANSPORT"] = "1"
    if not password_is_set():
        print("OMG_RESTIC_PASSWORD is not set", file=sys.stderr)
        return EXIT_USAGE
    password = require_password()
    paths = get_paths()
    try:
        policy = _policy(paths)
    except OmgError as exc:
        print(redact(exc.message), file=sys.stderr)
        return EXIT_USAGE
    repo = repo_path(policy)
    if repo is None:
        print("backup_repo is empty", file=sys.stderr)
        return EXIT_USAGE
    timeout = _timeout(policy)
    try:
        stopped = handle_locks(paths, repo, password=password, timeout=timeout, check=check, now=now,
                               hostname=hostname, pid_alive_fn=pid_alive_fn, runner=runner)
    except BackupFailed as exc:
        if exc.reason == "password_rejected":
            print("restic password was rejected", file=sys.stderr)
            return EXIT_PASSWORD
        print(redact(exc.message), file=sys.stderr)
        return EXIT_VERIFY
    if stopped is not None:
        return stopped
    staging = Path(tempfile.mkdtemp(prefix="omg-restore-", dir="/tmp"))
    try:
        listed = call_restic(paths, repo, ["snapshots", "--json"], password=password, timeout=timeout, runner=runner)
        if listed.returncode == 12:
            print("restic password was rejected", file=sys.stderr)
            return EXIT_PASSWORD
        if listed.returncode != 0:
            print("could not list snapshots", file=sys.stderr)
            return EXIT_NO_SNAPSHOT if listed.returncode == 10 else EXIT_VERIFY
        try:
            snapshots = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            print("snapshot list was not json", file=sys.stderr)
            return EXIT_VERIFY
        if not snapshots:
            print("no snapshot in the repository", file=sys.stderr)
            return EXIT_NO_SNAPSHOT
        restored = call_restic(paths, repo, ["restore", "latest", "--host", HOST, "--tag", "omg",
                                            "--target", str(staging)],
                              password=password, timeout=timeout, runner=runner)
        if restored.returncode != 0:
            print("restic restore failed", file=sys.stderr)
            return EXIT_VERIFY
        try:
            root = find_snapshot_root(staging)
        except IntegrityError_:
            print("snapshot must contain exactly one database", file=sys.stderr)
            return EXIT_VERIFY
        return recover_from_tree(paths, root, check=check)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        register_secret(password)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg not in ("--check",) for arg in args):
        print("usage: python3 -m oh_my_grok.recover [--check]", file=sys.stderr)
        return EXIT_USAGE
    try:
        return run_recover(check="--check" in args or os.environ.get("OMG_RECOVER_CHECK") == "1")
    except Exception as exc:  # noqa: BLE001 - the wrapper must not print a traceback that might quote a secret
        print(f"internal error: {redact(str(exc))[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
