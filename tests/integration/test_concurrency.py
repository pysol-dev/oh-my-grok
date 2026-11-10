"""§10 items 2, 5, 18 using REAL separate OS processes against one SQLite DB (WAL + BEGIN IMMEDIATE)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oh_my_grok.errors import Conflict

WORKER = str(Path(__file__).with_name("_worker.py"))


def spawn(args):
    return subprocess.Popen([sys.executable, WORKER, *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=os.environ.copy())


def last_json(p):
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def test_item2_parallel_explorer_librarian_no_lost_updates(harness, tmp_path):
    e = harness.e
    jobs = [harness.submit()["job_id"] for _ in range(3)]
    go = tmp_path / "go"
    procs = []
    for job in jobs:
        for kind in ("explore", "research"):
            t = harness.task(job, kind)
            procs.append(spawn(["claim_complete", job, t["id"], t["current_dispatch_id"], harness.clock(), go, kind]))
    time.sleep(0.5)
    go.write_text("go")
    outs = [last_json(p) for p in procs]
    assert all(o.get("completed") == "passed" for o in outs), outs
    assert len({o["pid"] for o in outs}) == 6
    for job in jobs:
        s = e.status(job, include_events=True)
        kinds = {t["kind"]: t["status"] for t in s["tasks"]}
        assert kinds["explore"] == kinds["research"] == "passed"
        assert kinds["analyze"] in ("queued", "dispatched")  # fan-in fired exactly once
        assert len([t for t in s["tasks"] if t["kind"] == "analyze"]) == 1
        assert len([ev for ev in s["events"] if ev["kind"] == "task_submitted"]) == 2
        pkt = harness.claim(job, "analyze")
        assert {a["kind"] for a in pkt["input_artifacts"]} >= {"explore", "research"}
    assert e.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert e.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_item5_two_processes_same_claim_exactly_one_wins(harness, tmp_path):
    job = harness.submit()["job_id"]
    t = harness.task(job, "explore")
    go = tmp_path / "go"
    procs = [spawn(["claim", job, t["id"], t["current_dispatch_id"], harness.clock(), go]) for _ in range(4)]
    time.sleep(0.5)
    go.write_text("go")
    outs = [last_json(p) for p in procs]
    winners = [o for o in outs if "claimed" in o and "error" not in o]
    assert len(winners) == 1, outs
    assert all(o.get("error") == "Conflict" for o in outs if o is not winners[0])
    n = harness.e._one("SELECT COUNT(*) c FROM task_attempts WHERE task_id=?", (t["id"],))["c"]
    assert n == 1


def test_item18_killed_worker_lease_expiry_recovers(harness, tmp_path):
    e = harness.e
    job = harness.submit()["job_id"]
    t = harness.task(job, "explore")
    p = spawn(["claim_and_hang", job, t["id"], t["current_dispatch_id"], harness.clock(), "-"])
    line = p.stdout.readline()
    dead_attempt = json.loads(line)["claimed"]
    os.kill(p.pid, signal.SIGKILL)  # the agent process dies mid-task
    p.wait(timeout=10)
    assert harness.task(job, "explore")["status"] == "working"
    harness.clock.advance(e.policy["lease_seconds"] + 5)
    rep = e.reconcile(job)
    assert rep["expired_leases"] >= 1
    t2 = harness.task(job, "explore")
    assert t2["status"] == "dispatched" and t2["current_dispatch_id"] != t["current_dispatch_id"]
    with pytest.raises(Conflict):  # the dead attempt can never land late
        e.complete(job, t["id"], dead_attempt, {"repo_map": "x", "files": [], "tests": [], "constraints": [],
                                                "questions": []})
    pkt = e.claim(job, t2["id"], t2["current_dispatch_id"])
    assert pkt["attempt_no"] == 2
    # a full engine restart (new connection) sees the same durable state
    e.close()
    from oh_my_grok.engine import Engine
    harness.e = Engine(transport=harness.transport, clock=harness.clock)
    assert harness.task(job, "explore")["status"] == "working"
