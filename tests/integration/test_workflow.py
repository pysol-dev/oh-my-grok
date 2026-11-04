"""Workflow invariants with simulated agents acting through the public Engine API (network mocked)."""
from pathlib import Path

import pytest

from omo_grok import fixture as fx
from omo_grok.errors import Conflict, GateRefused, ValidationError


def test_full_fixture_job_reaches_done_via_gates(harness):
    job, sha, res = harness.full_fixture_job()
    assert res["final_gate"]["passed"] is True
    s = harness.e.status(job)
    assert s["phase"] == "done" and s["final_result_revision"] == sha
    assert "soft_approval" in s["assurance"]
    vs = [v for v in s["verifications"] if v["revision"] == sha]
    assert {v["purpose"] for v in vs} == {"candidate", "final"} and all(v["status"] == "passed" for v in vs)


def test_item3_artifact_flow_metis_plan_executor(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.research(job)
    pkt = harness.claim(job, "analyze")
    contents = sorted(open(a["path"]).read() for a in pkt["input_artifacts"])
    assert contents == ["EXPLORER FINDINGS: add() returns a-b", "LIBRARIAN: unittest docs v3.13"]
    with pytest.raises(ValidationError):  # Metis must acknowledge BOTH inputs by hash
        e.complete(job, pkt["task_id"], pkt["attempt_id"],
                   {"consumed_artifacts": [pkt["input_artifacts"][0]["sha256"]], "gap_matrix": [], "risks": [],
                    "missing_decisions": [], "suggested_criteria": []},
                   [{"kind": "analysis", "name": "m.md", "content": "M"}])
    e.complete(job, pkt["task_id"], pkt["attempt_id"],
               {"consumed_artifacts": [a["sha256"] for a in pkt["input_artifacts"]], "gap_matrix": [], "risks": [],
                "missing_decisions": [], "suggested_criteria": []},
               [{"kind": "analysis", "name": "m.md", "content": "METIS SYNTHESIS"}])
    plan_pkt = harness.claim(job, "plan")
    kinds = {a["kind"]: open(a["path"]).read() for a in plan_pkt["input_artifacts"]}
    assert kinds["analysis"] == "METIS SYNTHESIS" and "explore" in kinds and "research" in kinds
    e.complete(job, plan_pkt["task_id"], plan_pkt["attempt_id"],
               {"work_units": ["w"], "acceptance_criteria": ["a"], "test_procedure": "t", "rollback": "r",
                "unknowns": []}, [{"kind": "plan", "name": "plan.md", "content": "THE APPROVED PLAN"}])
    harness.review_plan(job)
    approved_hash = e.plan_show(job)["current"]["plan_hash"]
    harness.approve(job)
    ipkt = harness.claim(job, "implement")
    rev = ipkt["plan_revision"]
    texts = [open(a["path"]).read() for a in ipkt["input_artifacts"] if a["kind"] == "plan_canonical"]
    assert texts == ["THE APPROVED PLAN\n"]
    assert ipkt["worktree_root"] and ipkt["branch"].startswith("omo/") and ipkt["base_revision"]
    for f in ["schema_version", "job_id", "task_id", "attempt_id", "dispatch_id", "role", "goal", "task_objective",
              "allowed_actions", "prohibited_actions", "read_only", "repo_root", "worktree_root", "base_revision",
              "input_artifacts", "acceptance_criteria", "output_schema", "deadline_or_lease", "retry_budget",
              "relevant_prior_failures"]:
        assert f in ipkt, f
    assert rev and rev["plan_hash"] == approved_hash  # Executor receives exactly the approved revision


def test_item4_two_jobs_isolated(harness):
    a = harness.submit()["job_id"]
    harness.research(a)
    a_before = harness.e.status(a)
    b = harness.submit()["job_id"]
    assert a != b
    a_after = harness.e.status(a)
    assert a_after["phase"] == a_before["phase"] and a_after["counts"] == a_before["counts"]
    tb = harness.task(b, "explore")
    ta = harness.task(a, "analyze")
    with pytest.raises(Conflict):  # cannot claim job A's task under job B
        harness.e.claim(b, ta["id"], ta["current_dispatch_id"])
    with pytest.raises(Conflict):  # B's dispatch id cannot claim A's task
        harness.e.claim(a, ta["id"], tb["current_dispatch_id"])


def test_item4_idempotent_submit(harness):
    r1 = harness.submit(key="same-key-123")
    r2 = harness.submit(key="same-key-123")
    assert r1["job_id"] == r2["job_id"] and r2["created"] is False
    with pytest.raises(Conflict):
        harness.e.submit("different goal", "same-key-123", mode="fixture")


def test_item6_duplicates_and_stale_rejections(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    t = harness.task(job, "explore")
    did = t["current_dispatch_id"]
    pkt = e.claim(job, t["id"], did)
    with pytest.raises(Conflict):  # duplicate webhook -> second claim with same dispatch rejected
        e.claim(job, t["id"], did)
    good = {"repo_map": "x", "files": [], "tests": [], "constraints": [], "questions": []}
    refs = [{"kind": "explore", "name": "e.md", "content": "x"}]
    with pytest.raises(ValidationError):  # wrong attempt id
        e.complete(job, t["id"], "not-a-uuid", good, refs)
    with pytest.raises(Conflict):
        e.complete(job, t["id"], "00000000-0000-4000-8000-000000000000", good, refs)
    other = harness.task(job, "research")
    with pytest.raises(Conflict):  # wrong task for this attempt
        e.complete(job, other["id"], pkt["attempt_id"], good, refs)
    r = e.complete(job, t["id"], pkt["attempt_id"], good, refs)
    assert r["task_status"] == "passed"
    with pytest.raises(Conflict):  # duplicate completion
        e.complete(job, t["id"], pkt["attempt_id"], good, refs)
    with pytest.raises(Conflict):  # invalid transition: claim a passed task
        e.claim(job, t["id"], did)


def test_item6_expired_lease_cannot_complete(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    pkt = harness.claim(job, "explore")
    harness.clock.advance(e.policy["lease_seconds"] + 1)
    with pytest.raises(Conflict, match="lease expired"):
        e.complete(job, pkt["task_id"], pkt["attempt_id"],
                   {"repo_map": "x", "files": [], "tests": [], "constraints": [], "questions": []},
                   [{"kind": "explore", "name": "e.md", "content": "x"}])
    rep = e.reconcile(job)
    assert rep["expired_leases"] == 1
    t = harness.task(job, "explore")
    assert t["status"] == "dispatched" and t["current_dispatch_id"] != pkt["dispatch_id"]
    with pytest.raises(Conflict):  # old attempt is stale forever
        e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], "late")


def test_checkpoint_renews_lease_and_is_not_completion(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    pkt = harness.claim(job, "explore")
    harness.clock.advance(e.policy["lease_seconds"] - 10)
    r = e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], "half way",
                     [{"kind": "explore", "name": "partial.md", "content": "p"}])
    assert r["completed"] is False and harness.task(job, "explore")["status"] == "working"
    harness.clock.advance(60)
    assert e.reconcile(job)["expired_leases"] == 0


def test_item9_plan_rejection_new_revision_and_old_approval_invalid(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.research(job)
    harness.analyze(job)
    harness.plan(job, "PLAN A")
    pkt, _ = harness.review_plan(job, verdict="reject")
    assert e.status(job)["phase"] == "planning"
    p2 = harness.claim(job, "plan")
    assert any(a["kind"] == "plan_canonical" for a in p2["input_artifacts"])  # rejected plan is context
    e.complete(job, p2["task_id"], p2["attempt_id"],
               {"work_units": ["w"], "acceptance_criteria": ["a"], "test_procedure": "t", "rollback": "r",
                "unknowns": []}, [{"kind": "plan", "name": "plan.md", "content": "PLAN B"}])
    h2 = e.plan_show(job)["current"]["plan_hash"]
    with pytest.raises(ValidationError):  # Momus must review the exact hash
        harness.review_plan(job, plan_hash="0" * 64)
    t = harness.task(job, "plan_review")
    e.complete(job, t["id"], t["current_attempt_id"], {"verdict": "accept", "plan_hash": h2, "findings": []})
    harness.approve(job, h2, allow_edit=False)
    assert e.status(job)["approval"] is not None
    # user denies after approval -> plan changes -> old approval invalid
    e.deny(job, "add more tests", actor="human")
    assert e.status(job)["approval"] is None
    assert e._one("SELECT COUNT(*) c FROM approvals WHERE job_id=? AND revoked_at IS NULL", (job,))["c"] == 0
    with pytest.raises(GateRefused):
        e.approve(job, h2, actor="human", evidence="x")


def test_item9_plan_loop_bounded(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.research(job)
    harness.analyze(job)
    for i in range(e.policy["max_plan_rounds"]):
        harness.plan(job, f"PLAN {i}")
        harness.review_plan(job, verdict="reject")
    s = e.status(job)
    assert s["phase"] == "blocked" and "plan review loop exhausted" in s["blocked_reason"]


def test_item11_no_implementation_before_approval_and_no_cross_job(harness):
    e = harness.e
    a = harness.submit()["job_id"]
    b = harness.submit()["job_id"]
    ha = harness.to_approval(a, "PLAN for job A")
    hb = harness.to_approval(b, "PLAN for job B")
    assert ha != hb
    assert harness.task(a, "implement") is None  # nothing to implement before approval
    assert e.status(a)["worktree"] is None
    with pytest.raises(GateRefused):  # job A's hash cannot approve job B
        e.approve(b, ha, actor="h", evidence="x")
    with pytest.raises(GateRefused):
        e.approve(a, "f" * 64, actor="h", evidence="x")
    harness.approve(a, ha)
    assert harness.task(a, "implement") is not None and harness.task(b, "implement") is None
    assert e._valid_approval(e._job(b)) is None  # A's receipt is bound to job A only
    assert e.status(b)["phase"] == "awaiting_approval" and hb


def test_item11_approve_wrong_phase_and_synthetic_only_for_fixture(harness, tmp_path):
    e = harness.e
    job = harness.submit()["job_id"]
    with pytest.raises(GateRefused):
        e.approve(job, "a" * 64, actor="h", evidence="x")
    from omo_grok import fixture as fx
    repo = tmp_path / "realrepo"
    fx.make_repo(repo)
    real = e.submit("real goal", "real-key-0001", repo=str(repo))
    assert real["mode"] == "plan_only"
    harness.to_approval(real["job_id"])
    with pytest.raises(GateRefused):
        e.record_synthetic_approval(real["job_id"], e.plan_show(real["job_id"])["current"]["plan_hash"])
    r = harness.approve(real["job_id"])  # soft approval of a plan_only job never authorizes edits
    assert r["scope"]["allow_edit"] is False and r["implementation_started"] is False
    assert e.status(real["job_id"])["worktree"] is None
    with pytest.raises(GateRefused):  # implement mode requires the repo allowlist
        e.submit("real goal", "real-key-0002", repo=str(repo), mode="implement")


def test_item12_fake_fix_fails_and_missing_verifier_blocks(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.to_approval(job)
    harness.approve(job)
    _, sha, res = harness.implement(job, fix="fake")
    assert res["verification"]["status"] == "failed"
    assert e.status(job)["phase"] == "running" and e.status(job)["repair_cycles"] == 1
    assert harness.task(job, "code_review") is None  # reviews never start on a failing build
    # stdlib-shadowing attack through the real engine path: committed unittest/ package printing VERIFIED, bug unfixed
    def shadow(wt):
        (wt / "unittest").mkdir()
        for f in ("__init__.py", "__main__.py"):
            (wt / "unittest" / f).write_text("import sys\nprint('VERIFIED')\nsys.exit(0)\n")
        return fx.commit_all(wt, "shadow stdlib unittest")
    _, _, res = harness.implement(job, fix=shadow)
    assert res["verification"]["status"] == "failed" and "shadowing" in res["verification"]["detail"]
    assert e.status(job)["repair_cycles"] == 2 and harness.task(job, "code_review") is None
    # job without a usable verifier profile: blocked, not passed
    j2 = harness.submit(verifier_profile="example-template-do-not-use")["job_id"]
    harness.to_approval(j2)
    harness.approve(j2)
    _, _, r2 = harness.implement(j2)
    assert r2["verification"]["status"] == "error"
    s = e.status(j2)
    assert s["phase"] == "blocked" and "fail closed" in s["blocked_reason"]


def test_item13_three_failed_cycles_block(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.to_approval(job)
    harness.approve(job)
    for i in range(3):
        _, _, res = harness.implement(job, fix="fake")
        assert res["verification"]["status"] == "failed"
    s = e.status(job)
    assert s["phase"] == "blocked" and s["blocked_reason"].startswith("repair_budget_exhausted")
    assert s["repair_cycles"] == 3
    assert len([t for t in s["tasks"] if t["kind"] == "implement"]) == 3
    assert e.reconcile(job)["scheduled"] == 0
    with pytest.raises(GateRefused):
        e.resume(job, actor="h")
    r = e.resume(job, actor="h", grant_repair_cycles=1)
    assert r["phase"] == "running"
    harness.implement(job)  # a real fix now passes
    assert harness.task(job, "code_review")["status"] == "dispatched"


def test_item14_review_and_qa_can_reject_passing_build(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.to_approval(job)
    harness.approve(job)
    _, sha1, res = harness.implement(job)
    assert res["verification"]["status"] == "passed"
    harness.review(job, "code_review", verdict="reject")
    s = e.status(job)
    assert s["repair_cycles"] == 1
    assert harness.task(job, "qa")["status"] == "cancelled"  # stale sibling superseded
    pkt = harness.claim(job, "implement")
    assert pkt["cycle"] == 2 and pkt["relevant_prior_failures"]
    from omo_grok import fixture as fx
    from pathlib import Path
    wt = Path(pkt["worktree_root"])
    (wt / "calc" / "review_notes.py").write_text('"""addressed review"""\n')  # source-only: .md is not allowed
    sha2 = fx.commit_all(wt, "address review")
    e.complete(job, pkt["task_id"], pkt["attempt_id"], {"commit_sha": sha2, "touched_files": ["calc/review_notes.py"],
               "summary": "s", "unit_evidence": "u", "deviations": [], "risks": []})
    assert sha2 != sha1
    vs = [v for v in e.status(job)["verifications"] if v["revision"] == sha2]
    assert vs and vs[0]["status"] == "passed"  # dependent gates re-run on the new revision
    harness.review(job, "code_review")
    harness.review(job, "qa", verdict="reject")
    assert e.status(job)["repair_cycles"] == 2


def test_item15_old_pass_cannot_approve_new_commit(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.to_approval(job)
    harness.approve(job)
    _, sha1, _ = harness.implement(job)
    pkt = harness.claim(job, "code_review")
    with pytest.raises(ValidationError):  # review must cite the exact candidate
        e.complete(job, pkt["task_id"], pkt["attempt_id"], {"verdict": "accept", "revision": "a" * 40,
                                                             "findings": []})
    e.complete(job, pkt["task_id"], pkt["attempt_id"], {"verdict": "accept", "revision": sha1, "findings": []})
    harness.review(job, "qa")
    # someone moves the branch after reviews (new unreviewed commit) before the final gate
    from omo_grok import fixture as fx
    from pathlib import Path
    wt = Path(e.status(job)["worktree"])
    (wt / "calc" / "__init__.py").write_text("def add(a, b):\n    return 0\n")
    fx.commit_all(wt, "sneaky change")
    _, res = harness.integrate(job)
    g = res["final_gate"]
    assert g["passed"] is False and g["checks"]["branch_head_is_candidate"]["ok"] is False
    assert e.status(job)["phase"] == "blocked"


def test_item15_final_verifier_runs_on_exact_revision(harness):
    job, sha, res = harness.full_fixture_job()
    fin = [v for v in harness.e.status(job)["verifications"] if v["purpose"] == "final"]
    assert len(fin) == 1 and fin[0]["revision"] == sha and fin[0]["status"] == "passed"


def test_independence_same_bot_refused_for_real_jobs_reported_for_fixture(harness):
    e = harness.e
    # rebind executor to Momus' bot (same as code-reviewer)
    e.bind_role("executor", "42b0d03a-d1fb-4fdc-87b1-35d9123f6f74", "Momus", routine_id="r-exec",
                independence_group="g-momus", max_concurrent=4, verified=True, provenance="test")
    job, sha, res = harness.full_fixture_job()
    g = res["final_gate"]
    assert g["passed"] is True and g["assurance"].startswith("reduced_non_independent")
    j2 = harness.submit()["job_id"]
    with __import__("omo_grok.db", fromlist=["tx"]).tx(e.conn):
        e.conn.execute("UPDATE jobs SET mode='implement' WHERE id=?", (j2,))  # simulate a real-repo job policy
    e.policy["fixture_allow_reduced_assurance"] = False
    harness.to_approval(j2)
    harness.approve(j2)
    harness.implement(j2)
    harness.review(j2, "code_review")
    harness.review(j2, "qa")
    _, r2 = harness.integrate(j2)
    assert r2["final_gate"]["passed"] is False and r2["final_gate"]["checks"]["independence"]["ok"] is False


def test_item19_cancelled_and_blocked_jobs_do_not_dispatch(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    n_calls = len(harness.transport.calls)
    r = e.cancel(job, "user changed mind", actor="mcp")
    assert r["phase"] == "cancelled"
    harness.clock.advance(10_000)
    rep = e.reconcile()
    assert rep["scheduled"] == 0 and len(harness.transport.calls) == n_calls
    t = harness.task(job, "explore")
    with pytest.raises(Conflict):
        e.claim(job, t["id"], t["current_dispatch_id"])
    with pytest.raises(Conflict):
        e.resume(job, actor="x")
    # blocked job: nothing dispatches until explicit resume
    j2 = harness.submit()["job_id"]
    t = harness.task(j2, "explore")
    with __import__("omo_grok.db", fromlist=["tx"]).tx(e.conn):
        e._block_task(t, "test block")
    assert e.status(j2)["phase"] == "blocked"
    research = harness.task(j2, "research")
    with pytest.raises(Conflict):
        e.claim(j2, research["id"], research["current_dispatch_id"])
    c0 = len(harness.transport.calls)
    harness.clock.advance(10_000)
    e.reconcile()
    assert len(harness.transport.calls) == c0
    e.resume(j2, actor="human")
    assert harness.task(j2, "explore")["status"] == "dispatched"


def test_mcp_cancel_of_real_work_requires_human(harness, tmp_path):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.to_approval(job)
    harness.approve(job)
    with __import__("omo_grok.db", fromlist=["tx"]).tx(e.conn):
        e.conn.execute("UPDATE jobs SET mode='implement' WHERE id=?", (job,))
    r = e.cancel(job, "agent wants to stop", actor="mcp")
    assert r.get("requires_human_confirmation") and e.status(job)["phase"] == "running"
    r = e.cancel(job, "human confirmed", actor="human", confirmed=True)
    assert r["phase"] == "cancelled" and r["worktree_preserved"]
    from pathlib import Path
    assert Path(r["worktree_preserved"]).exists()


def test_hephaestus_awaiting_resume_never_self_webhooks(harness):
    e = harness.e
    job = harness.submit()["job_id"]
    harness.research(job)
    harness.analyze(job)
    t = harness.task(job, "plan")
    assert t["status"] == "awaiting_resume"
    assert all("hephaestus" not in c["url"] for c in harness.transport.calls)
    d = e._one("SELECT * FROM dispatch_outbox WHERE id=?", (t["current_dispatch_id"],))
    assert d["channel"] == "local"
    assert "hephaestus" in e.status(job)["next_action"]
    harness.plan(job)
    assert e.status(job)["phase"] == "plan_review"


def test_role_binding_rules(harness):
    e = harness.e
    with pytest.raises(ValidationError):
        e.bind_role("explorer", "00000000-0000-4000-8000-000000000000", "Ghost", provenance="t")
    with pytest.raises(ValidationError):
        e.bind_role("explorer", "dbbe6828-c2fb-4474-8fe3-a0e6df942fc6", "Explorer", verified=True, provenance="t")
    job = harness.submit()["job_id"]
    harness.claim(job, "explore")
    with pytest.raises(Conflict):  # binding cannot change during an active attempt
        e.bind_role("explorer", "dbbe6828-c2fb-4474-8fe3-a0e6df942fc6", "Explorer", routine_id="r2", verified=True,
                    provenance="t")
    v = e.list_bindings()
    assert {b["role"] for b in v} == {"hephaestus", "explorer", "librarian", "metis", "momus", "executor",
                                      "code-reviewer", "qa-executor"}


def test_cancel_confirmation_checked_inside_write_txn_race_with_approval(harness, monkeypatch):
    """A concurrent approval landing between MCP cancel's start and its write lock must still force confirmation."""
    from omo_grok import db as dbmod
    from omo_grok import engine as engmod
    from omo_grok.engine import Engine
    e = harness.e
    job = harness.submit()["job_id"]
    ph = harness.to_approval(job)
    with dbmod.tx(e.conn):
        e.conn.execute("UPDATE jobs SET mode='implement' WHERE id=?", (job,))
    other = Engine(transport=harness.transport, clock=harness.clock)  # separate connection = concurrent actor
    real_tx = dbmod.tx
    fired = []

    def racing_tx(conn):
        if conn is e.conn and not fired:
            fired.append(1)
            other.approve(job, ph, actor="human", evidence="concurrent")  # commits before cancel's BEGIN IMMEDIATE
        return real_tx(conn)

    monkeypatch.setattr(engmod.db, "tx", racing_tx)
    r = e.cancel(job, "agent wants to stop", actor="mcp")
    monkeypatch.setattr(engmod.db, "tx", real_tx)
    assert fired and r.get("requires_human_confirmation"), r
    assert e.status(job)["phase"] in ("plan_approved", "running")
    other.close()


def test_item9_approval_of_rev_a_cannot_authorize_rev_b_implementation(harness):
    """Approve revision A; the plan then changes to revision B through the engine (_on_plan revokes): the approval of
    A is invalid, A's hash cannot be re-approved, and B's implementation claim is refused until B itself is approved."""
    e = harness.e
    job = harness.submit()["job_id"]
    ha = harness.to_approval(job, "PLAN REV A")
    harness.approve(job, ha, allow_edit=False)  # approved, implementation not yet started (plan_approved)
    assert e._valid_approval(e._job(job)) is not None
    rev_a = e._job(job)["current_plan_revision_id"]
    e.deny(job, "scope changed", actor="human")  # -> replan
    p2 = harness.claim(job, "plan")
    e.complete(job, p2["task_id"], p2["attempt_id"],
               {"work_units": ["w"], "acceptance_criteria": ["a"], "test_procedure": "t", "rollback": "r",
                "unknowns": []}, [{"kind": "plan", "name": "plan.md", "content": "PLAN REV B"}])
    jb = e._job(job)
    assert jb["current_plan_revision_id"] != rev_a
    assert e._valid_approval(jb) is None and e.status(job)["approval"] is None
    assert e._one("SELECT COUNT(*) c FROM approvals WHERE job_id=? AND revoked_at IS NULL", (job,))["c"] == 0
    hb = e.plan_show(job)["current"]["plan_hash"]
    assert hb != ha
    t = harness.task(job, "plan_review")
    pr = e.claim(job, t["id"], t["current_dispatch_id"])
    e.complete(job, pr["task_id"], pr["attempt_id"], {"verdict": "accept", "plan_hash": hb, "findings": []})
    with pytest.raises(GateRefused):  # A's hash cannot authorize B
        e.approve(job, ha, actor="human", evidence="x")
    assert harness.task(job, "implement") is None  # nothing to claim: no implementation without B's approval
    # forge an implement task for B (as if scheduled by mistake): the claim is refused by the approval gate
    from omo_grok import db
    with db.tx(e.conn):
        tid = e._new_task(job, "implement")
        e._schedule(job)
    impl = e._one("SELECT * FROM tasks WHERE id=?", (tid,))
    assert impl["status"] == "queued" and impl["current_dispatch_id"] is None  # scheduler refuses to dispatch it
    did = "00000000-0000-4000-8000-00000000000b"  # publish a dispatch by hand, as an attacker with DB access might
    with db.tx(e.conn):
        e.conn.execute("INSERT INTO dispatch_outbox(id,job_id,task_id,role,channel,attempt,idempotency_key,status,"
                       "next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,'webhook',0,?,'pending',0,?,?)",
                       (did, job, tid, impl["role"], f"forged-{tid}", db.utcnow(), db.utcnow()))
        e.conn.execute("UPDATE tasks SET status='dispatched', current_dispatch_id=? WHERE id=?", (did, tid))
    impl = e._one("SELECT * FROM tasks WHERE id=?", (tid,))
    with pytest.raises(GateRefused):
        e.claim(job, tid, impl["current_dispatch_id"])


def test_item9_late_plan_submission_while_running_is_refused(harness):
    """After approval starts implementation, a late planner submission cannot swap in a new revision."""
    from omo_grok import db
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job, "PLAN REV A"))
    rev_a = e._job(job)["current_plan_revision_id"]
    with db.tx(e.conn):
        e._new_task(job, "plan", inputs=[], cycle=9)
        e._schedule(job)
    p2 = harness.claim(job, "plan")
    with pytest.raises(Conflict):
        e.complete(job, p2["task_id"], p2["attempt_id"],
                   {"work_units": ["w"], "acceptance_criteria": ["a"], "test_procedure": "t", "rollback": "r",
                    "unknowns": []}, [{"kind": "plan", "name": "plan.md", "content": "PLAN REV B"}])
    assert e._job(job)["current_plan_revision_id"] == rev_a and e._valid_approval(e._job(job)) is not None


def test_item9_plan_change_while_implementer_working_rejects_claim_and_submit(harness):
    """Engine plan-change revocation: even a task that slipped past cancellation (dispatched or working) is refused at
    claim AND at complete once the approval no longer matches the current plan revision."""
    from omo_grok import db
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job, "PLAN REV A"))
    impl = harness.task(job, "implement")
    with db.tx(e.conn):  # revision B appears without going through deny (approval row left un-revoked)
        cur = e._one("SELECT * FROM plan_revisions WHERE id=?", (e._job(job)["current_plan_revision_id"],))
        e.conn.execute("INSERT INTO plan_revisions(id,job_id,round,artifact_id,content_hash,author_role,submitted_at,"
                       "review_status) VALUES ('rev-b',?,?,?,?,?,?,'accepted')",
                       (job, cur["round"] + 1, cur["artifact_id"], "b" * 64, "metis", db.utcnow()))
        e.conn.execute("UPDATE jobs SET current_plan_revision_id='rev-b' WHERE id=?", (job,))
    assert e._one("SELECT COUNT(*) c FROM approvals WHERE job_id=? AND revoked_at IS NULL", (job,))["c"] == 1
    assert e._valid_approval(e._job(job)) is None
    with pytest.raises(GateRefused):
        e.claim(job, impl["id"], impl["current_dispatch_id"])
    assert harness.task(job, "implement")["status"] == "dispatched"  # refused claim changed nothing
    # restore rev A, claim legitimately, then switch to B mid-work: complete is refused
    e.conn.execute("UPDATE jobs SET current_plan_revision_id=? WHERE id=?", (cur["id"], job))
    pkt = e.claim(job, impl["id"], impl["current_dispatch_id"])
    sha = fx.apply_real_fix(Path(pkt["worktree_root"]))
    e.conn.execute("UPDATE jobs SET current_plan_revision_id='rev-b' WHERE id=?", (job,))
    with pytest.raises(GateRefused):
        e.complete(job, pkt["task_id"], pkt["attempt_id"],
                   {"commit_sha": sha, "touched_files": ["calc/__init__.py"], "summary": "fix",
                    "unit_evidence": "ran unittest", "deviations": [], "risks": []})
    assert harness.task(job, "implement")["status"] == "working"


def _to_reviews(harness):
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    harness.implement(job)
    return job


@pytest.mark.parametrize("kind", ["code_review", "qa"])
@pytest.mark.parametrize("how", ["revoked", "plan_changed", "null_plan_revision"])
def test_review_and_qa_claims_are_approval_gated(harness, kind, how):
    """code_review and qa claims are refused unless the approval of the CURRENT plan revision is live and the task is
    bound to that revision; a refused claim changes nothing."""
    from omo_grok import db
    e = harness.e
    job = _to_reviews(harness)
    t = harness.task(job, kind)
    assert t["status"] == "dispatched" and t["plan_revision_id"] == e._job(job)["current_plan_revision_id"]
    with db.tx(e.conn):
        if how == "revoked":  # approval revoked out-of-band, task left dispatched
            e.conn.execute("UPDATE approvals SET revoked_at=? WHERE job_id=?", (db.utcnow(), job))
        elif how == "plan_changed":
            cur = e._one("SELECT * FROM plan_revisions WHERE id=?", (e._job(job)["current_plan_revision_id"],))
            e.conn.execute("INSERT INTO plan_revisions(id,job_id,round,artifact_id,content_hash,author_role,"
                           "submitted_at,review_status) VALUES ('rev-x',?,?,?,?,?,?,'accepted')",
                           (job, cur["round"] + 1, cur["artifact_id"], "c" * 64, "metis", db.utcnow()))
            e.conn.execute("UPDATE jobs SET current_plan_revision_id='rev-x' WHERE id=?", (job,))
        else:
            e.conn.execute("UPDATE tasks SET plan_revision_id=NULL WHERE id=?", (t["id"],))
    with pytest.raises(GateRefused):
        e.claim(job, t["id"], t["current_dispatch_id"])
    assert harness.task(job, kind)["status"] == "dispatched"


def test_plan_bound_tasks_always_carry_a_plan_revision(harness):
    job = _to_reviews(harness)
    harness.review(job, "code_review")
    harness.review(job, "qa")
    rows = harness.e._all("SELECT kind, plan_revision_id FROM tasks WHERE job_id=? AND kind IN "
                          "('implement','code_review','qa','integrate')", (job,))
    assert {r["kind"] for r in rows} >= {"implement", "code_review", "qa"}
    assert all(r["plan_revision_id"] for r in rows), rows


def test_implement_packet_fails_closed_without_plan_revision(harness):
    from omo_grok import db
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    t = harness.task(job, "implement")
    with db.tx(e.conn):
        e.conn.execute("UPDATE tasks SET plan_revision_id=NULL WHERE id=?", (t["id"],))
    with pytest.raises(GateRefused):
        e.claim(job, t["id"], t["current_dispatch_id"])
    with pytest.raises(GateRefused):  # and the packet builder itself refuses
        e._packet(job, t["id"])


def test_submit_rejects_unknown_verifier_profile(harness):
    with pytest.raises(ValidationError):
        harness.submit(verifier_profile="no-such-profile")
    with pytest.raises(ValidationError):
        harness.submit(verifier_profile=["fixture-calc-unittest"])
    assert harness.submit(verifier_profile="fixture-calc-unittest")["created"]
