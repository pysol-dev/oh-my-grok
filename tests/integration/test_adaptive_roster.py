"""Intent gating and adaptive roster, driven through the public Engine API with simulated agents (network mocked).
Acceptance tests for R1, R2, R3 and R12 (spec/ADAPTIVE.md section 11)."""
import json
import os
from pathlib import Path

import pytest

from omo_grok import fixture as fx
from omo_grok import gitops
from omo_grok.errors import Conflict, GateRefused, ValidationError

BASE = {"files_estimate": 1, "new_dependency": False, "external_api": False, "security_sensitive": False,
        "cross_cutting": False}


def S(**kw):
    return {**BASE, **kw}


def _enable(mode_map):
    cfg = Path(os.environ["OMO_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol["features"] = mode_map
    (cfg / "policy.json").write_text(json.dumps(pol))


@pytest.fixture
def h(omo_home):
    from helpers import Harness
    _enable({"adaptive_roster": "enforce"})
    hh = Harness()
    yield hh
    hh.close()


@pytest.fixture
def legacy(omo_home):
    from helpers import Harness
    hh = Harness()
    yield hh
    hh.close()


def kinds(h, job):
    return sorted(r["kind"] for r in h.e._all("SELECT kind FROM tasks WHERE job_id=?", (job,)))


def sub(h, intent="implementation", scope=None, mode="fixture", **kw):
    return h.submit(mode=mode, intent=intent, scope=scope if scope is not None else S(), **kw)


# --------------------------------------------------------------------------- R1 intent gating
def test_explanation_creates_nothing(h):
    before = h.e._one("SELECT (SELECT COUNT(*) FROM jobs) j, (SELECT COUNT(*) FROM tasks) t")
    r = h.e.submit("What does calc.add do?", "explain-key-0001", intent="explanation")
    assert r == {"created": False, "roster_class": "none", "action": "answer_directly"}
    after = h.e._one("SELECT (SELECT COUNT(*) FROM jobs) j, (SELECT COUNT(*) FROM tasks) t")
    assert (before["j"], before["t"]) == (after["j"], after["t"])
    ev = h.e._all("SELECT job_id, kind FROM events WHERE kind='intent_gated'")
    assert len(ev) == 1 and ev[0]["job_id"] is None


def test_explanation_is_gated_even_with_every_feature_off(legacy):
    r = legacy.e.submit("explain it", "explain-key-0002", intent="explanation")
    assert r["created"] is False and legacy.e._one("SELECT COUNT(*) c FROM jobs")["c"] == 0


def test_investigation_runs_read_only_and_finishes_without_a_plan(h):
    job = h.e.submit("Why is add() wrong?", "investigate-0001", intent="investigation",
                     scope=S(external_api=False))["job_id"]
    assert kinds(h, job) == ["analyze", "explore"]
    row = h.e._job(job)
    assert row["intent"] == "investigation" and row["mode"] == "plan_only"
    h.do(job, "explore", {"repo_map": "m", "files": ["calc/__init__.py:1"], "tests": [], "constraints": [],
                          "questions": []}, [{"kind": "explore", "name": "e.md", "content": "EXPLORE"}])
    assert h.e._job(job)["phase"] == "analysis"
    h.analyze(job)
    s = h.e.status(job)
    assert s["phase"] == "done" and s["assurance"] == "read_only_investigation"
    assert kinds(h, job) == ["analyze", "explore"]
    row = h.e._job(job)
    assert row["worktree_path"] is None and row["candidate_revision"] is None
    assert h.e._one("SELECT COUNT(*) c FROM approvals WHERE job_id=?", (job,))["c"] == 0
    assert h.e._one("SELECT COUNT(*) c FROM plan_revisions WHERE job_id=?", (job,))["c"] == 0


def test_investigation_with_external_api_includes_research(h):
    job = h.e.submit("Which API?", "investigate-0002", intent="investigation",
                     scope=S(external_api=True))["job_id"]
    assert kinds(h, job) == ["analyze", "explore", "research"]


def test_investigation_without_scope_researches_conservatively(h):
    job = h.e.submit("Which API?", "investigate-0003", intent="investigation")["job_id"]
    assert kinds(h, job) == ["analyze", "explore", "research"]


@pytest.mark.parametrize("mode", ["implement", "fixture"])
def test_read_only_point_1_submit_forces_plan_only(h, mode):
    with pytest.raises(ValidationError):
        h.e.submit("look", "investigate-0010", intent="investigation", mode=mode, scope=S())
    assert h.e._one("SELECT COUNT(*) c FROM jobs")["c"] == 0


def test_read_only_points_2_3_4_new_task_approve_and_start(h):
    job = h.e.submit("look", "investigate-0011", intent="investigation", scope=S())["job_id"]
    for kind in ("plan", "plan_review", "implement", "code_review", "qa", "integrate"):
        with pytest.raises(GateRefused):
            with __import__("omo_grok.db", fromlist=["tx"]).tx(h.e.conn):
                h.e._new_task(job, kind, plan_revision_id="x")
    with pytest.raises(GateRefused, match="read-only"):
        h.e.approve(job, "0" * 64, actor="t", evidence="e", allow_edit=True)
    with pytest.raises(GateRefused, match="read-only"):
        h.e._start_implementation(job)
    assert h.e._job(job)["worktree_path"] is None


def test_intent_is_enforced_without_the_roster_flag(legacy):
    with pytest.raises(ValidationError):
        legacy.e.submit("look", "investigate-0012", intent="investigation", mode="fixture")
    job = legacy.e.submit("look", "investigate-0013", intent="investigation")["job_id"]
    assert legacy.e._job(job)["intent"] == "investigation"


def test_unknown_intent_refused(h):
    with pytest.raises(ValidationError):
        h.e.submit("x", "bad-intent-0001", intent="refactor-everything")


def test_enforce_requires_intent_and_scope(h):
    with pytest.raises(ValidationError, match="intent"):
        h.e.submit("x", "needs-intent-01", mode="plan_only")
    with pytest.raises(ValidationError, match="scope"):
        h.e.submit("x", "needs-scope-001", mode="plan_only", intent="implementation")
    with pytest.raises(ValidationError):
        h.e.submit("x", "needs-scope-002", mode="plan_only", intent="implementation", scope={"files_estimate": 1})
    assert h.e._one("SELECT COUNT(*) c FROM jobs")["c"] == 0


# --------------------------------------------------------------------------- R2 roster
def test_floor_below_is_refused_and_creates_nothing(h):
    with pytest.raises(ValidationError) as ei:
        sub(h, scope=S(security_sensitive=True), roster_class="quick")
    assert ei.value.details["floor"] == "heavy"
    assert h.e._one("SELECT COUNT(*) c FROM jobs")["c"] == 0


def test_floor_selects_the_class_and_a_larger_class_is_allowed(h):
    a = sub(h, scope=S())["job_id"]
    b = sub(h, scope=S(), roster_class="standard")["job_id"]
    c = sub(h, scope=S(new_dependency=True))["job_id"]
    assert [h.e._job(j)["roster_class"] for j in (a, b, c)] == ["quick", "standard", "heavy"]


def test_quick_task_set(h):
    r = sub(h, scope=S())
    job = r["job_id"]
    assert kinds(h, job) == ["plan"] and set(r["task_ids"]) == {"plan"}
    assert r["phase"] == "planning" and r["roster_class"] == "quick"
    assert h.task(job, "plan")["status"] == "awaiting_resume"


def test_standard_task_set_without_and_with_external_api(h):
    a = sub(h, scope=S(files_estimate=5))["job_id"]
    assert kinds(h, a) == ["analyze", "explore", "plan"]
    b = sub(h, scope=S(files_estimate=5, external_api=True))["job_id"]
    assert kinds(h, b) == ["analyze", "explore", "plan", "research"]
    deps = h.e._all("SELECT t.kind k, d.kind dk FROM task_deps x JOIN tasks t ON t.id=x.task_id "
                    "JOIN tasks d ON d.id=x.depends_on WHERE t.job_id=?", (b,))
    assert sorted((r["k"], r["dk"]) for r in deps) == [("analyze", "explore"), ("analyze", "research"),
                                                       ("plan", "analyze")]


def test_heavy_equals_legacy_dag(h, omo_home):
    r = sub(h, scope=S(new_dependency=True))
    job = r["job_id"]
    assert set(r["task_ids"]) == {"explore", "research", "analyze", "plan"} and r["phase"] == "research"
    deps = h.e._all("SELECT t.kind k, d.kind dk FROM task_deps x JOIN tasks t ON t.id=x.task_id "
                    "JOIN tasks d ON d.id=x.depends_on WHERE t.job_id=?", (job,))
    assert sorted((r["k"], r["dk"]) for r in deps) == [("analyze", "explore"), ("analyze", "research"),
                                                       ("plan", "analyze")]


def test_quick_class_full_pipeline_reaches_done_through_every_gate(h):
    job = sub(h, scope=S())["job_id"]
    h.plan(job)
    h.review_plan(job)
    assert h.e._job(job)["phase"] == "awaiting_approval"
    h.approve(job)
    _, sha, _ = h.implement(job)
    h.review(job, "code_review")
    h.review(job, "qa")
    _, res = h.integrate(job)
    assert res["final_gate"]["passed"] is True
    s = h.e.status(job)
    assert s["phase"] == "done" and s["final_result_revision"] == sha
    assert kinds(h, job) == ["code_review", "implement", "integrate", "plan", "plan_review", "qa"]
    checks = res["final_gate"]["checks"]
    for name in ("plan_reviewed", "approval_matches_plan_hash", "independent_code_review_and_qa",
                 "candidate_verification_passed", "final_verification_passed_on_exact_revision"):
        assert checks[name]["ok"], name


def test_quick_class_still_needs_momus_and_approval(h):
    job = sub(h, scope=S())["job_id"]
    h.plan(job)
    with pytest.raises(GateRefused):  # no approval without Momus acceptance
        h.e.approve(job, h.e.plan_show(job)["current"]["plan_hash"], actor="t", evidence="e")
    h.review_plan(job)
    with pytest.raises(GateRefused):  # wrong hash
        h.e.approve(job, "0" * 64, actor="t", evidence="e")


def test_standard_class_full_pipeline(h):
    job = sub(h, scope=S(files_estimate=5))["job_id"]
    h.do(job, "explore", {"repo_map": "m", "files": ["a:1"], "tests": [], "constraints": [], "questions": []},
         [{"kind": "explore", "name": "e.md", "content": "EXPLORE"}])
    assert h.e._job(job)["phase"] == "analysis"
    h.analyze(job)
    assert h.e._job(job)["phase"] == "planning"
    h.plan(job)
    h.review_plan(job)
    h.approve(job)
    h.implement(job)
    h.review(job, "code_review")
    h.review(job, "qa")
    _, res = h.integrate(job)
    assert res["final_gate"]["passed"] is True


def test_flags_off_keeps_the_legacy_dag_even_with_intent_and_scope(legacy):
    job = legacy.submit(intent="implementation", scope=S())["job_id"]
    assert kinds(legacy, job) == ["analyze", "explore", "plan", "research"]
    row = legacy.e._job(job)
    assert row["intent"] == "implementation" and row["roster_class"] == "heavy"


def test_flags_off_without_intent_is_the_pre_existing_behaviour(legacy):
    r = legacy.submit()
    job = r["job_id"]
    assert kinds(legacy, job) == ["analyze", "explore", "plan", "research"]
    row = legacy.e._job(job)
    assert row["intent"] is None and row["roster_class"] is None
    assert "roster_class" not in r


def test_shadow_mode_records_the_decision_without_changing_behaviour(omo_home):
    from helpers import Harness
    _enable({"adaptive_roster": "shadow"})
    hh = Harness()
    try:
        job = hh.submit(intent="implementation", scope=S())["job_id"]
        assert kinds(hh, job) == ["analyze", "explore", "plan", "research"]
        ev = hh.e._one("SELECT data_json FROM events WHERE job_id=? AND kind='roster_shadow'", (job,))
        d = json.loads(ev["data_json"])
        assert d["would_be_class"] == "quick" and d["would_create"] == ["plan"]
    finally:
        hh.close()


def test_idempotency_key_reuse_with_different_intent_conflicts(h):
    sub(h, key="same-key-intent-1", scope=S())
    with pytest.raises(Conflict):
        sub(h, key="same-key-intent-1", scope=S(files_estimate=2))
    again = sub(h, key="same-key-intent-1", scope=S())
    assert again["created"] is False


# --------------------------------------------------------------------------- R3 escalation
def test_escalation_revokes_approval_replans_and_finishes(h):
    job = sub(h, scope=S())["job_id"]
    h.plan(job)
    h.review_plan(job)
    h.approve(job)
    old_rev = h.e._job(job)["current_plan_revision_id"]
    impl = h.task(job, "implement")
    assert impl["status"] in ("dispatched", "queued")
    r = h.e.escalate(job, "standard", "needs analysis of calc callers", actor="hephaestus")
    assert r["from_class"] == "quick" and r["to_class"] == "standard"
    row = h.e._job(job)
    assert row["roster_class"] == "standard" and row["phase"] == "research"
    assert h.e._valid_approval(row) is None
    assert h.e._one("SELECT review_status FROM plan_revisions WHERE id=?", (old_rev,))["review_status"] == "superseded"
    assert h.e._one("SELECT status FROM tasks WHERE id=?", (impl["id"],))["status"] == "cancelled"
    assert h.e._one("SELECT COUNT(*) c FROM escalations WHERE job_id=?", (job,))["c"] == 1
    # a claim of the cancelled implementation task is impossible
    with pytest.raises(Conflict):
        h.e.claim(job, impl["id"], impl["current_dispatch_id"])
    h.do(job, "explore", {"repo_map": "m", "files": ["a:1"], "tests": [], "constraints": [], "questions": []},
         [{"kind": "explore", "name": "e2.md", "content": "EXPLORE AFTER ESCALATION"}])
    h.analyze(job)
    h.plan(job, "PLAN v2 after escalation")
    h.review_plan(job)
    h.approve(job)
    new_impl = h.task(job, "implement", "dispatched")
    assert new_impl["id"] != impl["id"] and new_impl["plan_revision_id"] == h.e._job(job)["current_plan_revision_id"]
    h.implement(job)
    h.review(job, "code_review")
    h.review(job, "qa")
    _, res = h.integrate(job)
    assert res["final_gate"]["passed"] is True and h.e.status(job)["phase"] == "done"


def test_escalate_down_or_sideways_or_unknown_is_refused(h):
    job = sub(h, scope=S(files_estimate=5))["job_id"]  # standard
    for target in ("quick", "standard", "none", "mega"):
        with pytest.raises(ValidationError):
            h.e.escalate(job, target, "x", actor="t")
    assert h.e._job(job)["roster_class"] == "standard"
    h.e.escalate(job, "heavy", "security review needed", actor="t")
    with pytest.raises(ValidationError):
        h.e.escalate(job, "heavy", "again", actor="t")


def test_escalation_requires_reason_and_a_live_adaptive_job(h):
    job = sub(h, scope=S())["job_id"]
    with pytest.raises(ValidationError):
        h.e.escalate(job, "standard", "  ", actor="t")
    inv = h.e.submit("look", "investigate-0020", intent="investigation", scope=S())["job_id"]
    with pytest.raises(ValidationError):
        h.e.escalate(inv, "heavy", "why not", actor="t")
    h.e.cancel(job, "stop", actor="t")
    with pytest.raises(Conflict):
        h.e.escalate(job, "standard", "too late", actor="t")


def test_reviews_from_a_superseded_plan_do_not_block_the_new_plan(h):
    """A candidate that already has code review and QA, then an escalation, must still reach integration.
    The cancelled reviews stay at cycle 1; the replacement implementation must not share that cycle."""
    job = sub(h, scope=S())["job_id"]
    h.plan(job)
    h.review_plan(job)
    h.approve(job)
    h.implement(job)
    old_reviews = h.e._all("SELECT id, cycle, status FROM tasks WHERE job_id=? AND kind IN ('code_review','qa')",
                           (job,))
    assert len(old_reviews) == 2 and {r["cycle"] for r in old_reviews} == {1}
    h.e.escalate(job, "standard", "the first candidate needs a wider look", actor="hephaestus")
    left = h.e._all("SELECT status FROM tasks WHERE job_id=? AND kind IN ('code_review','qa') AND cycle=1", (job,))
    assert {r["status"] for r in left} == {"cancelled"}
    h.do(job, "explore", {"repo_map": "m", "files": ["a:1"], "tests": [], "constraints": [], "questions": []},
         [{"kind": "explore", "name": "e2.md", "content": "EXPLORE AFTER ESCALATION"}])
    h.analyze(job)
    h.plan(job, "PLAN v2 after a reviewed candidate")
    h.review_plan(job)
    h.approve(job)
    new_impl = h.task(job, "implement", "dispatched")
    assert new_impl["cycle"] > 1
    h.implement(job)
    h.review(job, "code_review")
    h.review(job, "qa")
    _, res = h.integrate(job)
    assert res["final_gate"]["passed"] is True and h.e.status(job)["phase"] == "done"


def test_repair_after_escalation_carries_the_current_plan(h):
    job = sub(h, scope=S(), max_repair_cycles=1)["job_id"]
    h.plan(job)
    h.review_plan(job)
    h.approve(job)
    old_art = h.e._one("SELECT artifact_id FROM plan_revisions WHERE id=?",
                       (h.e._job(job)["current_plan_revision_id"],))["artifact_id"]
    h.e.escalate(job, "standard", "replace the plan before repairing", actor="hephaestus")
    h.do(job, "explore", {"repo_map": "m", "files": ["a:1"], "tests": [], "constraints": [], "questions": []},
         [{"kind": "explore", "name": "e2.md", "content": "EXPLORE AFTER ESCALATION"}])
    h.analyze(job)
    h.plan(job, "PLAN v2 the repair must read")
    h.review_plan(job)
    h.approve(job)
    new_art = h.e._one("SELECT artifact_id FROM plan_revisions WHERE id=?",
                       (h.e._job(job)["current_plan_revision_id"],))["artifact_id"]
    assert new_art != old_art
    h.implement(job, fix="fake")
    assert h.e.status(job)["phase"] == "blocked"
    h.e.resume(job, actor="hephaestus", grant_repair_cycles=1)
    repair = h.task(job, "implement", "dispatched")
    ins = {r["artifact_id"] for r in h.e._all("SELECT artifact_id FROM task_inputs WHERE task_id=?", (repair["id"],))}
    assert new_art in ins and old_art not in ins
    assert repair["plan_revision_id"] == h.e._job(job)["current_plan_revision_id"]


def test_escalation_to_heavy_adds_the_research_task(h):
    job = sub(h, scope=S(files_estimate=5))["job_id"]  # standard, no research
    assert "research" not in kinds(h, job)
    h.e.escalate(job, "heavy", "unclear external API", actor="t")
    assert "research" in kinds(h, job)
    research = h.task(job, "research")
    assert research["status"] in ("dispatched", "queued")
    assert h.e._job(job)["phase"] == "research"


def test_auto_escalation_when_the_diff_adds_a_dependency(h):
    job = sub(h, scope=S())["job_id"]
    h.plan(job)
    h.review_plan(job)
    h.approve(job)

    def fix(wt):
        (wt / "requirements.txt").write_text("left-pad==1.0\n")
        p = wt / "calc" / "__init__.py"
        p.write_text(p.read_text().replace(fx.FIX_FROM, fx.FIX_TO).replace(fx.FAKE_LINES, ""))
        return fx.commit_all(wt, "fix add and add a dependency")

    _, sha, res = h.implement(job, fix=fix)
    row = h.e._job(job)
    assert row["roster_class"] == "standard" and row["phase"] == "research"
    assert h.e._valid_approval(row) is None
    impl = h.e._one("SELECT * FROM tasks WHERE job_id=? AND kind='implement' ORDER BY created_at LIMIT 1", (job,))
    assert impl["status"] == "rejected" and "scope" in impl["block_reason"]
    assert h.e._one("SELECT COUNT(*) c FROM tasks WHERE job_id=? AND kind='code_review'", (job,))["c"] == 0
    assert gitops.branch_head(Path(row["worktree_path"]), row["branch"]) == sha  # the commit is never touched
    e = h.e._one("SELECT * FROM escalations WHERE job_id=?", (job,))
    assert e["automatic"] == 1 and "requirements.txt" in e["observed_json"]
    assert h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='auto_escalation'", (job,))


def test_auto_escalation_when_the_diff_is_more_than_twice_the_attestation(h):
    job = sub(h, scope=S(files_estimate=1)).get("job_id")
    h.plan(job)
    h.review_plan(job)
    h.approve(job)

    def fix(wt):
        for n in ("a", "b", "c"):
            (wt / f"extra_{n}.txt").write_text(n)
        p = wt / "calc" / "__init__.py"
        p.write_text(p.read_text().replace(fx.FIX_FROM, fx.FIX_TO).replace(fx.FAKE_LINES, ""))
        return fx.commit_all(wt, "wide change")

    h.implement(job, fix=fix)
    assert h.e._job(job)["roster_class"] == "standard"


def test_honest_diff_does_not_escalate(h):
    job = sub(h, scope=S(files_estimate=1)).get("job_id")
    h.plan(job)
    h.review_plan(job)
    h.approve(job)
    h.implement(job)
    assert h.e._job(job)["roster_class"] == "quick"
    assert h.e._one("SELECT COUNT(*) c FROM escalations WHERE job_id=?", (job,))["c"] == 0
    assert h.task(job, "code_review") is not None


def test_heavy_job_with_contradicting_diff_is_flagged_but_not_blocked(h):
    job = sub(h, scope=S(new_dependency=True, files_estimate=1))["job_id"]
    h.to_approval(job)
    h.approve(job)

    def fix(wt):
        for n in "abc":
            (wt / "calc" / f"mod_{n}.py").write_text("X = 1\n")
        p = wt / "calc" / "__init__.py"
        p.write_text(p.read_text().replace(fx.FIX_FROM, fx.FIX_TO).replace(fx.FAKE_LINES, ""))
        return fx.commit_all(wt, "wide")

    h.implement(job, fix=fix)
    assert h.e._job(job)["roster_class"] == "heavy"
    assert h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='scope_contradiction_at_max_class'", (job,))
    assert h.task(job, "code_review") is not None


def test_only_an_investigation_can_finish_from_the_analysis_phase(h):
    from omo_grok import db
    job = sub(h, scope=S(files_estimate=5))["job_id"]
    row = h.e._job(job)
    with db.tx(h.e.conn):
        row = h.e._set_job(row, "analysis")
        with pytest.raises(Conflict, match="investigation"):
            h.e._set_job(row, "done")
