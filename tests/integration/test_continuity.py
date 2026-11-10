"""Boulder continuity: restart/resume semantics, duplicate-job prevention, repo lease (spec/ADAPTIVE.md 6.5, R4, R5)."""
import json
import os
from pathlib import Path

import pytest

from oh_my_grok import fixture as fx
from oh_my_grok import gitops
from oh_my_grok.errors import GateRefused
from oh_my_grok.paths import get_paths

BASE = {"files_estimate": 1, "new_dependency": False, "external_api": False, "security_sensitive": False,
        "cross_cutting": False}


def _policy(**extra):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol.update(extra)
    (cfg / "policy.json").write_text(json.dumps(pol))


@pytest.fixture
def h(omg_home):
    from helpers import Harness
    _policy(features={"boulder": "on"})
    hh = Harness()
    yield hh
    hh.close()


def boulder(job):
    return json.loads((get_paths().job_dir(job) / ".omo" / "boulder.json").read_text())


def test_restart_resumes_the_same_job_and_plan(h):
    from oh_my_grok.engine import Engine
    from helpers import FakeTransport, Clock
    job = h.submit()["job_id"]
    h.research(job)
    h.analyze(job)
    h.plan(job, "THE PLAN BEFORE RESTART")
    phase = h.e._job(job)["phase"]
    h.e.close()  # the process dies; only SQLite and the files survive
    e2 = Engine(transport=FakeTransport(), clock=Clock())
    h.e = e2
    s = e2.status()
    (aw,) = [w for w in s["active_work"] if w["job_id"] == job]
    assert aw["phase"] == phase == "plan_review" and aw["plan_name"] == f"omg-{job[:8]}"
    assert aw["boulder_path"] == str(get_paths().job_dir(job) / ".omo" / "boulder.json")
    assert aw["next_action"]
    assert "THE PLAN BEFORE RESTART" in Path(boulder(job)["active_plan"]).read_text()
    # the new process carries on with the very same job; it creates no second job
    h.review_plan(job)
    assert e2._one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    assert e2.status(job)["phase"] == "awaiting_approval"


def test_identical_request_resumes_instead_of_duplicating(h):
    a = h.submit(key="first-key-0001")
    b = h.submit(key="second-key-0002")  # different idempotency key, same request
    assert b["job_id"] == a["job_id"] and b["created"] is False and b["resumed"] is True
    assert h.e._one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    assert h.e._one("SELECT COUNT(*) c FROM tasks")["c"] == 4
    c = h.e.submit("A different goal entirely", "third-key-0003", mode="plan_only")
    assert c["created"] is True and c["job_id"] != a["job_id"]


def test_terminal_jobs_are_not_resumed(h):
    a = h.submit(key="first-key-0011")["job_id"]
    h.e.cancel(a, "abandon", actor="t")
    b = h.submit(key="second-key-0012")
    assert b["created"] is True and b["job_id"] != a


def test_duplicate_of_a_blocked_job_returns_it_but_does_not_unblock(h):
    a = h.submit(key="first-key-0021")["job_id"]
    pkt = h.claim(a, "explore")
    h.e.complete(a, pkt["task_id"], pkt["attempt_id"], {"status": "blocked", "reason": "no access"})
    b = h.submit(key="second-key-0022")
    assert b["job_id"] == a and b["resumed"] is True and b["phase"] == "blocked"
    assert "omg resume" in b["next_action"]
    assert h.e._job(a)["phase"] == "blocked"
    (aw,) = [w for w in h.e.status()["active_work"] if w["job_id"] == a]
    assert aw["phase"] == "blocked" and "omg resume" in aw["next_action"]
    h.e.resume(a, actor="t")
    assert h.e._job(a)["phase"] != "blocked"


def test_without_the_boulder_feature_duplicates_are_still_created(omg_home):
    from helpers import Harness
    hh = Harness()
    try:
        a = hh.submit(key="first-key-0031")["job_id"]
        b = hh.submit(key="second-key-0032")["job_id"]
        assert a != b
    finally:
        hh.close()


def test_deleting_or_editing_the_files_never_alters_the_database(h):
    import shutil
    job = h.submit()["job_id"]
    h.research(job)
    before = h.e.status(job)
    shutil.rmtree(get_paths().job_dir(job) / ".omo")
    after = h.e.status(job)
    for k in ("phase", "counts", "next_action", "plan"):
        assert before[k] == after[k]
    h.analyze(job)  # engine works without the files and re-creates them
    assert boulder(job)["x_omg"]["phase"] == "planning"


def test_completed_work_is_not_active_work(h):
    job, _, _ = h.full_fixture_job()
    assert all(w["job_id"] != job for w in h.e.status()["active_work"])


def test_active_work_lists_per_task_sessions(h):
    job = h.submit()["job_id"]
    pkt = h.claim(job, "explore")
    (aw,) = [w for w in h.e.status()["active_work"] if w["job_id"] == job]
    (ts,) = aw["sessions"]
    assert ts["attempt_id"] == pkt["attempt_id"] and ts["status"] == "running" and ts["task_kind"] == "explore"


# --------------------------------------------------------------------------- repo lease
@pytest.fixture
def two_jobs(omg_home, tmp_path):
    repo = tmp_path / "shared-repo"
    fx.make_repo(repo)
    top = str(gitops.toplevel(repo))
    return top


def _mk(mode, omg_home, top):
    from helpers import Harness
    _policy(features={"repo_lease": mode}, repo_allowlist=[top])
    return Harness()


def _approved_job(hh, top, key):
    job = hh.e.submit("Fix calc.add in the shared repo", key, repo=top, mode="implement",
                      verifier_profile="fixture-calc-unittest")["job_id"]
    hh.to_approval(job)
    return job


def test_repo_lease_enforce_refuses_a_second_editing_job_without_side_effects(omg_home, two_jobs):
    hh = _mk("enforce", omg_home, two_jobs)
    try:
        a = _approved_job(hh, two_jobs, "lease-job-0001")
        b = _approved_job(hh, two_jobs, "lease-job-0002-other")
        hh.approve(a)
        with pytest.raises(GateRefused, match="lease"):
            hh.approve(b)
        row = hh.e._job(b)
        assert row["phase"] == "awaiting_approval" and row["worktree_path"] is None
        assert hh.e._one("SELECT COUNT(*) c FROM approvals WHERE job_id=?", (b,))["c"] == 0
        assert hh.e._one("SELECT job_id FROM repo_leases")["job_id"] == a
        hh.e.cancel(a, "done with it", actor="t", confirmed=True)
        assert hh.e._one("SELECT COUNT(*) c FROM repo_leases")["c"] == 0
        hh.approve(b)
        assert hh.e._one("SELECT job_id FROM repo_leases")["job_id"] == b
    finally:
        hh.close()


def test_repo_lease_shadow_records_the_conflict_and_allows(omg_home, two_jobs):
    hh = _mk("shadow", omg_home, two_jobs)
    try:
        a = _approved_job(hh, two_jobs, "lease-job-0011")
        b = _approved_job(hh, two_jobs, "lease-job-0012-other")
        hh.approve(a)
        hh.approve(b)
        assert hh.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='repo_lease_conflict'", (b,))
        assert hh.e._one("SELECT COUNT(*) c FROM repo_leases")["c"] == 1
    finally:
        hh.close()


def test_repo_lease_is_released_when_the_job_finishes(omg_home, two_jobs):
    hh = _mk("enforce", omg_home, two_jobs)
    try:
        a = _approved_job(hh, two_jobs, "lease-job-0021")
        hh.approve(a)
        hh.implement(a)
        hh.review(a, "code_review")
        hh.review(a, "qa")
        hh.integrate(a)
        assert hh.e._job(a)["phase"] == "done"
        assert hh.e._one("SELECT COUNT(*) c FROM repo_leases")["c"] == 0
    finally:
        hh.close()


def test_repo_lease_off_does_not_touch_the_table(omg_home, two_jobs):
    hh = _mk("off", omg_home, two_jobs)
    try:
        a = _approved_job(hh, two_jobs, "lease-job-0031")
        hh.approve(a)
        assert hh.e._one("SELECT COUNT(*) c FROM repo_leases")["c"] == 0
    finally:
        hh.close()
