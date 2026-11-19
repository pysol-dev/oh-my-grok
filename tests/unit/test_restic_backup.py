"""Restic backup schedule, store failures, doctor, and restore decisions."""
import errno
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oh_my_grok import db, restic_backup, watchdog
from oh_my_grok.config import load_policy
from oh_my_grok.engine import Engine
from oh_my_grok.errors import ValidationError
from oh_my_grok.paths import get_paths
from oh_my_grok.restic_backup import (
    DOCTOR_FILE_LIMIT,
    STORE_FILE_CAP,
    BackupFailed,
    CommandResult,
    backup_due,
    classify_locks,
    doctor_report,
    parse_restic_time,
    relate_streams,
)
from oh_my_grok.transport import post_json

PASSWORD = "omg-test-restic-password"
HOOKS = b"HOOKSECRET_ZZ_DO_NOT_PRINT_0123456789"
NOW = 1_800_000_000.0


class FakeStore:
    """A directory that behaves like the measured agent store: 16 MiB hard cap, chmod refused."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.chmod_calls = 0

    def write(self, rel: str, data: bytes) -> None:
        if len(data) > STORE_FILE_CAP:
            raise OSError(errno.EFBIG, "File too large")
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def chmod(self, rel: str, mode: int) -> None:
        self.chmod_calls += 1
        raise OSError(errno.EPERM, "Operation not permitted")


def _policy(repo, **extra):
    path = Path(os.environ["OMG_CONFIG_DIR"]) / "policy.json"
    data = json.loads(path.read_text())
    features = extra.pop("features", {})
    data["features"] = features
    if repo is not None:
        data["backup_repo"] = str(repo)
    data.update(extra)
    path.write_text(json.dumps(data))


def _events(conn):
    return [dict(row) for row in conn.execute(
        "SELECT kind, data_json FROM events WHERE kind IN ('backup_failed', 'backup_ok') ORDER BY id")]


def _add_event(conn, kind="note", data="x"):
    conn.execute(
        "INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) VALUES (?,?,?,?,?,?,?)",
        (db.utcnow(), None, None, kind, "test", None, json.dumps({"v": data})))


def test_fake_store_refuses_one_byte_over_16_mib_and_refuses_chmod(tmp_path):
    store = FakeStore(tmp_path / "store")
    store.write("ok", b"abc")
    with pytest.raises(OSError) as exc:
        store.write("big", b"\0" * (STORE_FILE_CAP + 1))
    assert exc.value.errno == errno.EFBIG
    with pytest.raises(OSError) as denied:
        store.chmod("ok", 0o755)
    assert denied.value.errno == errno.EPERM
    assert store.chmod_calls == 1


def test_schedule_backs_up_on_generation_change_after_10_min_or_every_6_hours():
    min_s, max_s = 600, 21600
    assert backup_due(last_at=None, last_gen=None, gen=0, now=NOW, min_interval=min_s, max_interval=max_s)
    assert not backup_due(last_at=NOW - 9 * 60, last_gen=3, gen=3, now=NOW, min_interval=min_s, max_interval=max_s)
    assert not backup_due(last_at=NOW - (max_s - 1), last_gen=3, gen=3, now=NOW,
                          min_interval=min_s, max_interval=max_s)
    assert backup_due(last_at=NOW - max_s, last_gen=3, gen=3, now=NOW, min_interval=min_s, max_interval=max_s)
    assert not backup_due(last_at=NOW - (min_s - 1), last_gen=3, gen=4, now=NOW,
                          min_interval=min_s, max_interval=max_s)
    assert backup_due(last_at=NOW - min_s, last_gen=3, gen=4, now=NOW, min_interval=min_s, max_interval=max_s)


def test_backup_repo_must_be_an_absolute_local_path_outside_the_checkout(omg_home, tmp_path):
    _policy(tmp_path / "repo")
    assert load_policy(Path(os.environ["OMG_CONFIG_DIR"]))["backup_repo"] == str(tmp_path / "repo")
    for bad in ("relative/repo", "s3:bucket/key", "https://example.invalid/repo", str(Path(__file__).resolve().parents[2] / "state")):
        with pytest.raises(ValidationError):
            _policy(bad)
            load_policy(Path(os.environ["OMG_CONFIG_DIR"]))


def test_streams_relate_as_prefix_newer_or_diverged():
    one = [{"id": 1, "ts": "t", "job_id": None, "task_id": None, "kind": "a", "actor": None,
            "correlation_id": None, "data_json": None}]
    two = one + [{**one[0], "id": 2, "kind": "b"}]
    other = [{**one[0], "kind": "other"}]
    assert relate_streams(one, one) == "equal"
    assert relate_streams(one, two) == "local_prefix"
    assert relate_streams(two, one) == "local_newer"
    assert relate_streams(one, other) == "diverged"


def _runner_factory(script):
    calls = []

    def runner(argv, env, timeout):
        calls.append((argv, env, timeout))
        return script(len(calls), argv, env)

    return calls, runner


def test_run_passes_pack_size_host_and_tags_and_hides_hooks(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    repo = tmp_path / "repo"
    _policy(repo, features={"backup": "on"}, backup_timeout_seconds=30)
    engine = Engine()
    _add_event(engine.conn, data="bus")
    secret_path = engine.paths.hooks_path
    secret_path.write_bytes(HOOKS)
    os.chmod(secret_path, 0o600)
    seen = {}

    def script(n, argv, env):
        assert PASSWORD not in " ".join(argv)
        assert "--password-command" not in argv
        assert "--password-file" not in argv
        assert env["RESTIC_PASSWORD_COMMAND"] == "printenv OMG_RESTIC_PASSWORD"
        assert "RESTIC_PASSWORD" not in env
        assert env["RESTIC_PACK_SIZE"] == "4"
        assert argv[argv.index("--pack-size") + 1] == "4"
        tree = Path(argv[-1])
        assert (tree / "state" / "secrets" / "hooks.json").read_bytes() == HOOKS
        assert list(tree.rglob("*-wal")) == []
        assert list(tree.rglob("*-shm")) == []
        seen["tags"] = [argv[i + 1] for i, token in enumerate(argv) if token == "--tag"]
        seen["host"] = argv[argv.index("--host") + 1]
        return CommandResult(0, stdout="snapshot saved\n")

    calls, runner = _runner_factory(script)
    result = restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    assert len(calls) == 1
    assert result["host"] == "omg-box"
    assert seen["host"] == "omg-box"
    assert "omg" in seen["tags"] and "schema:7" in seen["tags"]
    assert any(tag.startswith("gen:") for tag in seen["tags"])
    blob = json.dumps(result)
    assert HOOKS.decode() not in blob and PASSWORD not in blob
    stored = " ".join((row["data_json"] or "") for row in _events(engine.conn))
    assert HOOKS.decode() not in stored and PASSWORD not in stored
    assert any(row["kind"] == "backup_ok" for row in _events(engine.conn))
    engine.close()


def test_efbig_is_recorded_once_and_not_retried(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    _policy(tmp_path / "repo", features={"backup": "on"})
    engine = Engine()

    def script(n, argv, env):
        return CommandResult(1, stderr="Save(<data/pack>) rename: file too large\n")

    calls, runner = _runner_factory(script)
    with pytest.raises(BackupFailed) as exc:
        restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    assert exc.value.reason == "efbig"
    assert len(calls) == 1
    failed = [row for row in _events(engine.conn) if row["kind"] == "backup_failed"]
    assert len(failed) == 1
    assert json.loads(failed[0]["data_json"])["reason"] == "efbig"
    assert HOOKS.decode() not in failed[0]["data_json"]
    again = restic_backup.run_backup(engine.conn, engine.paths, force=False, now=NOW + 30, runner=runner)
    assert again["reason"] == "efbig"
    assert len(calls) == 1
    engine.close()


def test_efbig_kills_a_hung_restic(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    monkeypatch.setenv("FAKE_RESTIC_STATE", str(tmp_path / "fake"))
    monkeypatch.setenv("OMG_RESTIC_BIN", str(Path(__file__).resolve().parents[1] / "fake_restic.py"))
    os.chmod(os.environ["OMG_RESTIC_BIN"], 0o755)
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake" / "mode").write_text("efbig\n")
    _policy(tmp_path / "repo", features={"backup": "on"}, backup_timeout_seconds=5)
    engine = Engine()
    started = time.monotonic()
    with pytest.raises(BackupFailed) as exc:
        restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW)
    assert exc.value.reason == "efbig"
    assert time.monotonic() - started < 4
    log = (tmp_path / "fake" / "log").read_text()
    assert log.count("\n") == 1
    assert PASSWORD not in log
    engine.close()


def test_eagain_and_lock_enoent_retry_once(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    _policy(tmp_path / "repo", features={"backup": "on"})
    engine = Engine()

    def eagain(n, argv, env):
        if n == 1:
            return CommandResult(1, stderr="rename: resource temporarily unavailable\n")
        return CommandResult(0, stdout="saved\n")

    calls, runner = _runner_factory(eagain)
    result = restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    assert result["ok"] is True and len(calls) == 2

    def always(n, argv, env):
        return CommandResult(1, stderr="rename: resource temporarily unavailable\n")

    calls, runner = _runner_factory(always)
    with pytest.raises(BackupFailed):
        restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    assert len(calls) == 2

    def enoent(n, argv, env):
        if n == 1:
            return CommandResult(1, stderr="List(locks) failed: open locks/abc: no such file or directory\n")
        return CommandResult(0, stdout="saved\n")

    calls, runner = _runner_factory(enoent)
    assert restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)["ok"] is True
    assert len(calls) == 2

    def other_enoent(n, argv, env):
        return CommandResult(1, stderr="open data/pack: no such file or directory\n")

    calls, runner = _runner_factory(other_enoent)
    with pytest.raises(BackupFailed) as exc:
        restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    assert exc.value.reason == "restic"
    assert len(calls) == 1
    engine.close()


def test_locked_repository_is_skipped_without_a_failure_event(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    _policy(tmp_path / "repo", features={"backup": "on"})
    engine = Engine()

    def script(n, argv, env):
        return CommandResult(11, stderr="repository is already locked\n")

    calls, runner = _runner_factory(script)
    result = restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    assert result["reason"] == "locked" and len(calls) == 1
    assert _events(engine.conn) == []
    engine.close()


def test_backup_does_not_chmod_the_repository(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    repo = tmp_path / "repo"
    repo.mkdir()
    _policy(repo, features={"backup": "on"})
    real = os.chmod

    def guard(path, mode):
        resolved = Path(path).resolve()
        if resolved == repo.resolve() or repo.resolve() in resolved.parents:
            raise AssertionError(f"chmod on the store: {path}")
        return real(path, mode)

    monkeypatch.setattr(os, "chmod", guard)
    engine = Engine()

    def script(n, argv, env):
        return CommandResult(0, stdout="saved\n")

    _calls, runner = _runner_factory(script)
    restic_backup.run_backup(engine.conn, engine.paths, force=True, now=NOW, runner=runner)
    engine.close()


def test_watchdog_runs_backup_only_when_the_flag_is_on(omg_home, tmp_path, monkeypatch):
    engine = Engine()
    try:
        off = watchdog.tick(engine, now=NOW)
        assert off["steps"]["backup"]["skipped"] is True
        assert off["steps"]["backup"]["reason"] == "backup off"
    finally:
        engine.close()
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    monkeypatch.setenv("FAKE_RESTIC_STATE", str(tmp_path / "fake"))
    monkeypatch.setenv("OMG_RESTIC_BIN", str(Path(__file__).resolve().parents[1] / "fake_restic.py"))
    os.chmod(os.environ["OMG_RESTIC_BIN"], 0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config").write_text("{}\n")
    _policy(repo, features={"backup": "on"}, backup_min_interval_seconds=600, backup_max_interval_seconds=21600)
    engine = Engine()
    try:
        first = watchdog.tick(engine, now=NOW)
        assert first["steps"]["backup"]["ok"] is True
        assert first["steps"]["backup"]["backup"]["skipped"] is False
        second = watchdog.tick(engine, now=NOW + 30)
        assert second["steps"]["backup"]["backup"]["reason"] == "schedule"
    finally:
        engine.close()


def test_doctor_flags_large_files_efbig_and_pack_size_without_the_password(omg_home, tmp_path, monkeypatch):
    monkeypatch.delenv("OMG_RESTIC_PASSWORD", raising=False)
    off = doctor_report(get_paths())
    assert all(item["ok"] for item in off)
    password = next(item for item in off if item["check"] == "backup_password")
    assert password["detail"] == "unset"
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", "SENTINEL-DO-NOT-LEAK-99")
    leaked = json.dumps(doctor_report(get_paths()))
    assert "SENTINEL-DO-NOT-LEAK-99" not in leaked
    assert '"detail": "set"' in leaked or '"detail":"set"' in leaked.replace(" ", "")

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    small = repo / "data" / "small"
    small.write_bytes(b"")
    os.truncate(small, DOCTOR_FILE_LIMIT - 1)
    monkeypatch.setenv("OMG_RESTIC_BIN", str(Path(__file__).resolve().parents[1] / "fake_restic.py"))
    os.chmod(os.environ["OMG_RESTIC_BIN"], 0o755)
    _policy(repo, features={"backup": "on"})
    (get_paths().state_dir / "backup").mkdir(parents=True)
    (get_paths().state_dir / "backup" / "status.json").write_text(json.dumps({"pack_size": 4}))
    ok = {item["check"]: item for item in doctor_report(get_paths())}
    assert ok["backup_files"]["ok"] is True
    assert ok["backup_pack_size"]["status"] == "PASS"
    assert "SENTINEL-DO-NOT-LEAK-99" not in json.dumps(ok)

    huge = repo / "data" / "huge"
    huge.write_bytes(b"")
    os.truncate(huge, DOCTOR_FILE_LIMIT)
    (get_paths().state_dir / "backup" / "status.json").write_text(json.dumps({"pack_size": 16}))
    engine = Engine()
    try:
        for _ in range(3):
            engine.conn.execute(
                "INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (db.utcnow(), None, None, "backup_failed", "backup", None, '{"reason":"restic"}'))
        engine.conn.execute(
            "INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) VALUES (?,?,?,?,?,?,?)",
            (db.utcnow(), None, None, "backup_failed", "backup", None, '{"reason":"efbig"}'))
    finally:
        engine.close()
    bad = {item["check"]: item for item in doctor_report(get_paths())}
    assert bad["backup_files"]["ok"] is False and bad["backup_files"]["status"] == "FAIL"
    assert bad["backup_failures"]["ok"] is False
    assert bad["backup_pack_size"]["status"] == "WARN" and bad["backup_pack_size"]["ok"] is True
    assert bad["backup_store_size"]["detail"].startswith("bytes=")
    assert "SENTINEL-DO-NOT-LEAK-99" not in json.dumps(bad)


def test_doctor_passes_again_after_a_successful_backup(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    repo = tmp_path / "repo"
    repo.mkdir()
    _policy(repo, features={"backup": "on"})
    (get_paths().state_dir / "backup").mkdir(parents=True)
    (get_paths().state_dir / "backup" / "status.json").write_text(json.dumps({"pack_size": 4}))
    engine = Engine()
    try:
        engine.conn.execute(
            "INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) VALUES (?,?,?,?,?,?,?)",
            (db.utcnow(), None, None, "backup_failed", "backup", None, '{"reason":"efbig"}'))
        engine.conn.execute(
            "INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) VALUES (?,?,?,?,?,?,?)",
            (db.utcnow(), None, None, "backup_ok", "backup", None, '{"generation":1}'))
        view = {item["check"]: item for item in doctor_report(get_paths(), runner=_quiet_runner())}
    finally:
        engine.close()
    assert view["backup_failures"]["ok"] is True


def _quiet_runner():
    def runner(argv, env, timeout):
        if "list" in argv:
            return CommandResult(0, stdout="")
        return CommandResult(0, stdout="[]\n")
    return runner


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_lock_time_parses_nanoseconds_from_restic():
    parsed = parse_restic_time("2026-11-19T12:40:56.180528794Z")
    assert parsed.year == 2026 and parsed.microsecond == 180528


def test_young_foreign_lock_retries_then_exits_16_without_remove_all(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    repo = tmp_path / "repo"
    repo.mkdir()
    _policy(repo, features={"backup": "on"})
    calls = []

    def runner(argv, env, timeout):
        calls.append(argv)
        if "list" in argv:
            return CommandResult(0, stdout="abc\n")
        if "cat" in argv:
            body = {"time": _stamp(NOW), "hostname": "otherhost", "pid": 4242, "exclusive": True}
            return CommandResult(0, stdout=json.dumps(body))
        if "--retry-lock" in argv:
            return CommandResult(11, stderr="repository is already locked\n")
        return CommandResult(0, stdout="ok\n")

    code = restic_backup_recover().handle_locks(
        get_paths(), repo, password=PASSWORD, timeout=5, check=False, now=NOW, hostname="thisbox",
        pid_alive_fn=lambda pid: False, runner=runner)
    assert code == 16
    assert not any("--remove-all" in argv for argv in calls)
    assert not any("unlock" in argv for argv in calls)
    assert any("--retry-lock" in argv and "2m" in argv for argv in calls)


def test_stale_lock_is_removed_with_plain_unlock_then_checked(omg_home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_RESTIC_PASSWORD", PASSWORD)
    repo = tmp_path / "repo"
    repo.mkdir()
    _policy(repo, features={"backup": "on"})
    calls = []

    def runner(argv, env, timeout):
        calls.append(argv)
        if "list" in argv:
            return CommandResult(0, stdout="abc\n")
        if "cat" in argv:
            body = {"time": _stamp(NOW - 7200), "hostname": "otherhost", "pid": 9, "exclusive": False}
            return CommandResult(0, stdout=json.dumps(body))
        return CommandResult(0, stdout="ok\n")

    code = restic_backup_recover().handle_locks(
        get_paths(), repo, password=PASSWORD, timeout=5, check=False, now=NOW, hostname="thisbox",
        pid_alive_fn=lambda pid: False, runner=runner)
    assert code is None
    unlocks = [argv for argv in calls if "unlock" in argv]
    assert len(unlocks) == 1 and "--remove-all" not in unlocks[0]
    assert any(argv[argv.index("check")] == "check" for argv in calls if "check" in argv)


def test_classify_live_local_lock_waits_and_dead_local_lock_is_stale():
    young = {"time": _stamp(NOW), "hostname": "box", "pid": 7}
    assert classify_locks([young], now=NOW, hostname="box", pid_alive_fn=lambda pid: True) == "wait"
    assert classify_locks([young], now=NOW, hostname="box", pid_alive_fn=lambda pid: False) == "stale"


def restic_backup_recover():
    from oh_my_grok import recover
    return recover


def _checkpoint(path: Path) -> None:
    conn = db.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()


def _snapshot_tree(root: Path, database: Path) -> Path:
    _checkpoint(database)
    tree = root / "tree"
    state = tree / "state"
    state.mkdir(parents=True)
    (tree / "jobs").mkdir()
    (tree / "jobs" / "note.txt").write_text("from-snapshot\n")
    target = state / "omg.sqlite3"
    target.write_bytes(database.read_bytes())
    return tree


def test_prefix_restore_migrates_and_keeps_a_newer_local_stream(omg_home, tmp_path):
    from oh_my_grok.recover import recover_from_tree
    engine = Engine()
    try:
        _add_event(engine.conn, data="one")
        snap = tmp_path / "snap.sqlite3"
        db.backup(engine.conn, snap)
        side = db.connect(snap)
        try:
            _add_event(side, data="two")
        finally:
            side.close()
        tree = _snapshot_tree(tmp_path, snap)
        paths = engine.paths
    finally:
        engine.close()
    assert recover_from_tree(paths, tree, check=False) == 0
    fresh = Engine()
    try:
        rows = fresh.conn.execute("SELECT data_json FROM events ORDER BY id").fetchall()
        assert [json.loads(row["data_json"])["v"] for row in rows] == ["one", "two"]
        assert (fresh.paths.jobs_dir / "note.txt").read_text() == "from-snapshot\n"
        assert db.current_version(fresh.conn) == 7
    finally:
        fresh.close()


def test_newer_local_data_is_kept_and_divergence_changes_nothing(omg_home, tmp_path):
    from oh_my_grok.recover import EXIT_DIVERGED, recover_from_tree
    engine = Engine()
    try:
        _add_event(engine.conn, data="one")
        snap = tmp_path / "snap.sqlite3"
        db.backup(engine.conn, snap)
        _add_event(engine.conn, data="local-only")
        tree = _snapshot_tree(tmp_path / "keep", snap)
        assert recover_from_tree(engine.paths, tree, check=False) == 0
        kept = [json.loads(row["data_json"])["v"]
                for row in engine.conn.execute("SELECT data_json FROM events ORDER BY id")]
        assert kept == ["one", "local-only"]
        assert not (engine.paths.jobs_dir / "note.txt").exists()

        other = tmp_path / "other.sqlite3"
        db.backup(engine.conn, other)
        side = db.connect(other)
        try:
            side.execute("DELETE FROM events")
            _add_event(side, data="unrelated")
        finally:
            side.close()
        before = engine.paths.db_path.read_bytes()
        diverged = _snapshot_tree(tmp_path / "diverged", other)
        assert recover_from_tree(engine.paths, diverged, check=False) == EXIT_DIVERGED
        assert engine.paths.db_path.read_bytes() == before
    finally:
        engine.close()


def test_newer_schema_is_refused_and_older_schema_migrates_forward(omg_home, tmp_path):
    from oh_my_grok.recover import EXIT_VERIFY, recover_from_tree
    engine = Engine()
    try:
        future = tmp_path / "future.sqlite3"
        db.backup(engine.conn, future)
        side = db.connect(future)
        try:
            side.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (99,'future','x',?)",
                (db.utcnow(),))
        finally:
            side.close()
        before = engine.paths.db_path.read_bytes()
        assert recover_from_tree(engine.paths, _snapshot_tree(tmp_path / "new", future), check=False) == EXIT_VERIFY
        assert engine.paths.db_path.read_bytes() == before
    finally:
        engine.close()

    old = tmp_path / "v6.sqlite3"
    conn = db.connect(old)
    try:
        db.migrate(conn, target=6)
        assert db.current_version(conn) == 6
    finally:
        conn.close()
    home = get_paths()
    home.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(home.db_path) + suffix)
        if extra.exists():
            extra.unlink()
    assert recover_from_tree(home, _snapshot_tree(tmp_path / "old", old), check=False) == 0
    check = db.connect(home.db_path)
    try:
        assert db.current_version(check) == 7
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_transport_disable_does_not_open_a_connection(monkeypatch):
    monkeypatch.setenv("OMG_DISABLE_TRANSPORT", "1")

    def boom(*args, **kwargs):
        raise AssertionError("transport was opened")

    monkeypatch.setattr("oh_my_grok.transport.http.client.HTTPSConnection", boom)
    monkeypatch.setattr("oh_my_grok.transport.http.client.HTTPConnection", boom)
    result = post_json("https://hooks.example.invalid/secret-path", "super-secret-key", {"a": 1}, 1, 1)
    assert result.error == "transport disabled"
    assert result.status is None


def test_recover_script_refuses_a_missing_password(monkeypatch):
    monkeypatch.delenv("OMG_RESTIC_PASSWORD", raising=False)
    import subprocess
    script = Path(__file__).resolve().parents[2] / "scripts" / "recover.sh"
    proc = subprocess.run([str(script)], capture_output=True, text=True, timeout=20, check=False, env=os.environ.copy())
    assert proc.returncode == 2
    assert "OMG_RESTIC_PASSWORD is not set" in proc.stderr
    assert PASSWORD not in proc.stderr
