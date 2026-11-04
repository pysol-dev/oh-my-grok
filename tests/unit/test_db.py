"""T02: schema, migrations, pragmas, CAS transition table, backup/restore (§10 item 18 part)."""
import sqlite3

import pytest

from omo_grok import db
from omo_grok.errors import Conflict
from omo_grok.states import check_dispatch, check_job, check_task


def test_migrations_apply_once_and_record_checksums(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    assert db.migrate(c, dry_run=True) == [1, 2]
    assert db.current_version(c) == 0
    assert db.migrate(c) == [1, 2]
    assert db.migrate(c) == []
    rows = c.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    assert [r[0] for r in rows] == [1, 2] and all(len(r[1]) == 64 for r in rows)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["jobs", "tasks", "task_attempts", "artifacts", "plan_revisions", "approvals", "role_bindings",
              "dispatch_outbox", "verifications", "reviews", "events", "hooks_meta", "gate_results"]:
        assert t in tables


def test_partial_migration_target(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    assert db.migrate(c, target=1) == [1]
    assert db.current_version(c) == 1
    assert db.migrate(c) == [2]


def test_checksum_mismatch_detected(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    db.migrate(c)
    c.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        db.migrate(c)


def test_pragmas(tmp_path):
    p = tmp_path / "x.sqlite3"
    c = db.connect(p)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert (p.stat().st_mode & 0o077) == 0


def test_foreign_keys_enforced(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    db.migrate(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO tasks(id,job_id,kind,role,status,created_at,updated_at) "
                  "VALUES ('t','missing-job','explore','explorer','queued','x','x')")


def test_tx_rolls_back_on_error(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    db.migrate(c)
    with pytest.raises(ValueError):
        with db.tx(c):
            c.execute("INSERT INTO hooks_meta(role,configured,updated_at) VALUES ('x',1,'t')")
            raise ValueError
    assert c.execute("SELECT COUNT(*) FROM hooks_meta").fetchone()[0] == 0


@pytest.mark.parametrize("src,dst", [("queued", "working"), ("passed", "queued"), ("cancelled", "queued"),
                                     ("submitted", "working"), ("dispatched", "passed")])
def test_illegal_task_transitions(src, dst):
    with pytest.raises(Conflict):
        check_task(src, dst)


def test_illegal_job_and_dispatch_transitions():
    with pytest.raises(Conflict):
        check_job("research", "done")
    with pytest.raises(Conflict):
        check_job("cancelled", "running")
    with pytest.raises(Conflict):
        check_dispatch("accepted", "sending")
    check_job("blocked", "running")
    check_dispatch("retry_wait", "sending")


def test_backup_and_restore_roundtrip(harness, tmp_path):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.research(job)
    before = e.status(job)
    bk = db.backup(e.conn, tmp_path / "bk.sqlite3")
    assert (bk.stat().st_mode & 0o077) == 0
    # damage the live DB state after the backup
    harness.analyze(job)
    with db.tx(e.conn):
        e.conn.execute("DELETE FROM events")
    e.close()
    db.restore(bk, e.paths.db_path)
    from omo_grok.engine import Engine
    e2 = Engine(transport=harness.transport, clock=harness.clock)
    after = e2.status(job)
    assert after["phase"] == before["phase"] and after["counts"] == before["counts"]
    assert e2._one("SELECT COUNT(*) c FROM events")["c"] > 0
    assert e2.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # restored job continues normally
    harness.e = e2
    harness.analyze(job)
    assert e2.status(job)["phase"] == "planning"
    e2.close()


def test_restore_rejects_corrupt_backup(tmp_path):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    with pytest.raises(Exception):
        db.restore(bad, tmp_path / "target.sqlite3")


def test_auto_backup_before_migrating_nonempty_db(omo_home):
    from omo_grok.engine import Engine
    from omo_grok.paths import get_paths
    p = get_paths()
    p.ensure()
    c = db.connect(p.db_path)
    db.migrate(c, target=1)
    c.close()
    e = Engine()
    assert db.current_version(e.conn) == 2
    assert list(p.backups_dir.glob("pre-migrate-v1-*.sqlite3"))
    e.close()
