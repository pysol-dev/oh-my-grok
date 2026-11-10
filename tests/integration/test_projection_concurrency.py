"""Multi-process and crash behaviour of the boulder writer (spec/ADAPTIVE.md 6.4, R6)."""
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oh_my_grok.paths import get_paths

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _policy(**extra):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol.update(extra)
    (cfg / "policy.json").write_text(json.dumps(pol))


def _env(**extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.update(extra)
    return env


def spawn(script, *args, **env):
    return subprocess.Popen([sys.executable, str(HERE / script), *map(str, args)], env=_env(**env),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(p, timeout=120):
    out, err = p.communicate(timeout=timeout)
    return p.returncode, out, err


@pytest.fixture
def h(omg_home):
    from helpers import Harness
    _policy(features={"boulder": "on"})
    hh = Harness()
    hh.clock.t = time.time()  # subprocess workers use the real clock
    yield hh
    hh.close()


def bpath(job):
    return get_paths().job_dir(job) / ".omo" / "boulder.json"


def max_event(h, job):
    return h.e._one("SELECT MAX(id) m FROM events WHERE job_id=?", (job,))["m"]


def test_concurrent_writers_lose_no_update_and_readers_never_see_a_partial_file(h, tmp_path):
    job = h.submit()["job_id"]
    pkt = h.claim(job, "explore")
    research = h.task(job, "research")
    stop = tmp_path / "stop"
    reader = spawn("_proj_reader.py", bpath(job), stop)
    workers = [spawn("_proj_worker.py", "checkpoint", job, pkt["task_id"], pkt["attempt_id"], 12) for _ in range(4)]
    workers.append(spawn("_proj_worker.py", "claim", job, research["id"], research["current_dispatch_id"]))
    results = [finish(w) for w in workers]
    stop.write_text("x")
    rc, out, err = finish(reader)
    assert all(r[0] == 0 for r in results), results
    assert rc == 0, out + err
    assert max_event(h, job) is not None
    final = json.loads(bpath(job).read_text())
    assert final["x_omg"]["generation"] == max_event(h, job)  # nothing lost
    assert len(final["session_ids"]) == 2  # both concurrent claims survived (rows, not read-modify-write)
    rec = h.e._one("SELECT * FROM boulder_projection WHERE job_id=?", (job,))
    assert rec["status"] == "ok" and rec["pending_sha256"] is None
    assert not h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind IN ('boulder_tamper','projection_stale')", (job,))
    assert not list(bpath(job).parent.rglob(".*.tmp"))


@pytest.mark.parametrize("point", ["after_lock", "after_snapshot", "after_temp_write", "after_replace",
                                   "after_db_record"])
def test_kill_minus_nine_at_every_writer_step_leaves_a_parseable_file_and_converges(h, point):
    job = h.submit()["job_id"]
    pkt = h.claim(job, "explore")
    before = json.loads(bpath(job).read_text())
    w = spawn("_proj_worker.py", "checkpoint", job, pkt["task_id"], pkt["attempt_id"], 1, OMG_FAULT_INJECT=point)
    rc, out, err = finish(w)
    assert rc == 137, (rc, out, err)
    mid = json.loads(bpath(job).read_text())  # complete old file or complete new file, never partial
    assert mid["x_omg"]["generation"] in (before["x_omg"]["generation"], max_event(h, job))
    h.e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], "after the crash")  # next flush converges
    final = json.loads(bpath(job).read_text())
    assert final["x_omg"]["generation"] == max_event(h, job)
    assert not h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='boulder_tamper'", (job,)), point
    assert h.e._one("SELECT status FROM boulder_projection WHERE job_id=?", (job,))["status"] == "ok"
    assert not list(bpath(job).parent.rglob(".*.tmp"))


@pytest.fixture
def quick_lock(omg_home):
    from helpers import Harness
    _policy(features={"boulder": "on"}, projection_lock_seconds=0.2)
    hh = Harness()
    hh.clock.t = time.time()  # subprocess workers use the real clock
    yield hh
    hh.close()


def test_lock_timeout_marks_stale_without_failing_the_job(quick_lock):
    h = quick_lock
    job = h.submit()["job_id"]
    lock = bpath(job).parent / "boulder.lock"
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        pkt = h.claim(job, "explore")  # the engine mutation itself succeeds
        assert pkt["attempt_id"]
        rec = h.e._one("SELECT * FROM boulder_projection WHERE job_id=?", (job,))
        assert rec["status"] == "stale" and "lock" in rec["last_error"]
        assert len(h.e._all("SELECT 1 FROM events WHERE job_id=? AND kind='projection_stale'", (job,))) == 1
        h.e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], "still working")
        assert len(h.e._all("SELECT 1 FROM events WHERE job_id=? AND kind='projection_stale'", (job,))) == 1
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    h.e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], "lock released")
    assert h.e._one("SELECT status FROM boulder_projection WHERE job_id=?", (job,))["status"] == "ok"
    assert json.loads(bpath(job).read_text())["x_omg"]["generation"] == max_event(h, job)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_directory_marks_stale_and_recovers(h):
    job = h.submit()["job_id"]
    d = bpath(job).parent
    d.chmod(0o500)
    try:
        pkt = h.claim(job, "explore")  # engine mutation still succeeds
        rec = h.e._one("SELECT * FROM boulder_projection WHERE job_id=?", (job,))
        assert rec["status"] == "stale" and rec["last_error"]
    finally:
        d.chmod(0o700)
    h.e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], "dir restored")
    assert h.e._one("SELECT status FROM boulder_projection WHERE job_id=?", (job,))["status"] == "ok"
    assert json.loads(bpath(job).read_text())["x_omg"]["generation"] == max_event(h, job)
    assert not h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='boulder_tamper'", (job,))
