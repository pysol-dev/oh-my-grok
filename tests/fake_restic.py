#!/usr/bin/env python3
"""Stand-in for the restic binary. Tests point OMG_RESTIC_BIN here.

It records argv and whether password environment variables were set, and it never writes the
password value. FAKE_RESTIC_STATE/mode selects a failure: efbig, eagain, enoent, lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

PASSWORD_COMMAND = "printenv OMG_RESTIC_PASSWORD"
VALUE_FLAGS = {
    "--repo", "--pack-size", "--cache-dir", "--retry-lock", "--password-command", "--password-file",
    "--repository-file", "--read-data-subset", "-r", "-p",
}


def state_dir() -> Path:
    raw = os.environ.get("FAKE_RESTIC_STATE")
    if not raw:
        sys.stderr.write("FAKE_RESTIC_STATE is not set\n")
        sys.exit(1)
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def record(state: Path) -> None:
    row = {
        "argv": sys.argv[1:],
        "password_command": os.environ.get("RESTIC_PASSWORD_COMMAND", ""),
        "restic_password_set": "RESTIC_PASSWORD" in os.environ,
        "restic_password_file_set": "RESTIC_PASSWORD_FILE" in os.environ,
        "omg_password_set": bool(os.environ.get("OMG_RESTIC_PASSWORD")),
        "pack_size_env": os.environ.get("RESTIC_PACK_SIZE", ""),
    }
    with (state / "log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def command_of(argv: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, argv[index + 1:]
    return "", []


def flag_value(argv: list[str], name: str) -> str | None:
    if name in argv:
        return argv[argv.index(name) + 1]
    return None


def require_password() -> None:
    if os.environ.get("RESTIC_PASSWORD") or os.environ.get("RESTIC_PASSWORD_FILE"):
        sys.stderr.write("refusing RESTIC_PASSWORD or RESTIC_PASSWORD_FILE\n")
        sys.exit(1)
    if "--password-command" in sys.argv or "--password-file" in sys.argv or "-p" in sys.argv:
        sys.stderr.write("password passed on argv\n")
        sys.exit(1)
    if os.environ.get("RESTIC_PASSWORD_COMMAND") != PASSWORD_COMMAND:
        sys.stderr.write("password command rejected\n")
        sys.exit(12)
    if not os.environ.get("OMG_RESTIC_PASSWORD"):
        sys.stderr.write("wrong password or no key found\n")
        sys.exit(12)


def repo_key(repo: str) -> str:
    return hashlib.sha256(repo.encode()).hexdigest()[:16]


def repo_state(state: Path, repo: str) -> Path:
    path = state / "repos" / repo_key(repo)
    path.mkdir(parents=True, exist_ok=True)
    return path


def apply_mode(state: Path) -> None:
    mode_path = state / "mode"
    mode = mode_path.read_text().strip() if mode_path.exists() else "ok"
    count_path = state / "attempts"
    count = int(count_path.read_text()) if count_path.exists() else 0
    if mode == "efbig":
        sys.stderr.write("Save(<data/pack>) rename: file too large\n")
        sys.stderr.flush()
        time.sleep(60)
        sys.exit(1)
    if mode == "eagain" and count < 1:
        count_path.write_text(str(count + 1))
        sys.stderr.write("rename: resource temporarily unavailable\n")
        sys.exit(1)
    if mode == "enoent" and count < 1:
        count_path.write_text(str(count + 1))
        sys.stderr.write("List(locks) failed: open locks/abc: no such file or directory\n")
        sys.exit(1)
    if mode == "lock":
        sys.stderr.write("repository is already locked by PID 1 on otherhost\n")
        sys.exit(11)
    if mode == "eagain-twice":
        count_path.write_text(str(count + 1))
        sys.stderr.write("rename: resource temporarily unavailable\n")
        sys.exit(1)


def copy_tree(src: Path, dest: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink():
                continue
            rel = path.resolve().relative_to(Path("/"))
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def latest_snap(repo_dir: Path) -> Path | None:
    root = repo_dir / "snaps"
    if not root.is_dir():
        return None
    snaps = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda item: int(item.name))
    return snaps[-1] if snaps else None


def do_backup(repo_dir: Path, args: list[str]) -> None:
    sources = [Path(arg) for arg in args if not arg.startswith("-")]
    if not sources:
        sys.stderr.write("no backup source\n")
        sys.exit(1)
    root = repo_dir / "snaps"
    root.mkdir(parents=True, exist_ok=True)
    number = len([path for path in root.iterdir() if path.is_dir()]) + 1
    dest = root / str(number)
    dest.mkdir()
    for source in sources:
        if source.is_dir():
            copy_tree(source, dest)
    (repo_dir / "config").write_text("{}\n")
    real_repo = flag_value(sys.argv[1:], "--repo")
    if real_repo:
        Path(real_repo).mkdir(parents=True, exist_ok=True)
        (Path(real_repo) / "config").write_text("{}\n")
    print(f"snapshot {number} saved")


def do_restore(repo_dir: Path, args: list[str]) -> None:
    target = flag_value(args, "--target") or flag_value(sys.argv[1:], "--target")
    if not target:
        sys.stderr.write("missing --target\n")
        sys.exit(1)
    snap = latest_snap(repo_dir)
    if snap is None:
        sys.stderr.write("no snapshot\n")
        sys.exit(10)
    copy_tree_out(snap, Path(target))


def copy_tree_out(src: Path, target: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        for name in filenames:
            path = current / name
            rel = path.relative_to(src)
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)


def locks(state: Path) -> list[dict]:
    path = state / "locks.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print("fake restic")
        return
    name, args = command_of(argv)
    if name == "version" or argv == ["version"]:
        print("restic 0.19.1 compiled with go1.26.4 on linux/amd64")
        return
    state = state_dir()
    record(state)
    if "--remove-all" in argv:
        (state / "remove_all").write_text("1\n")
    require_password()
    apply_mode(state)
    repo = flag_value(argv, "--repo") or ""
    repo_dir = repo_state(state, repo)
    if name == "init":
        (repo_dir / "config").write_text("{}\n")
        if repo:
            Path(repo).mkdir(parents=True, exist_ok=True)
            (Path(repo) / "config").write_text("{}\n")
        print("created restic repository")
        return
    if name == "backup":
        do_backup(repo_dir, args)
        return
    if name in ("forget", "prune", "check", "unlock"):
        if name == "check" and locks(state):
            sys.stderr.write("repository is already locked\n")
            sys.exit(11)
        if name == "unlock":
            lock_path = state / "locks.json"
            if lock_path.exists():
                lock_path.unlink()
        if name == "check":
            print("check ok")
        return
    if name == "list" and args and args[0] == "locks":
        for item in locks(state):
            print(item.get("id", ""))
        return
    if name == "cat" and args and args[0] == "lock":
        wanted = args[1]
        for item in locks(state):
            if item.get("id") == wanted:
                print(json.dumps({k: v for k, v in item.items() if k != "id"}))
                return
        sys.stderr.write("lock not found\n")
        sys.exit(1)
    if name == "snapshots":
        snap = latest_snap(repo_dir)
        if snap is None:
            print("[]")
            return
        print(json.dumps([{"short_id": snap.name, "id": snap.name}]))
        return
    if name == "restore":
        do_restore(repo_dir, args)
        return
    sys.stderr.write(f"unhandled restic command {name}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
