"""Language-server gate wired into the engine (candidate verification and the exact-revision final gate), driven
through the public Engine API with simulated agents. The servers are real; only the agents are simulated."""
import json
import os
from pathlib import Path

from oh_my_grok import fixture as fx
from test_lsp_real_servers import need

TYPE_ERROR = "\n\ndef label() -> int:\n    return 'not an int'\n"


def enable(mode):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol["features"] = {"lsp": mode} if mode else {}
    (cfg / "policy.json").write_text(json.dumps(pol))


def fixed(extra=""):
    def f(wt):
        p = wt / "calc" / "__init__.py"
        p.write_text(p.read_text().replace(fx.FIX_FROM, fx.FIX_TO).replace(fx.FAKE_LINES, "")
                     .replace(TYPE_ERROR, "") + extra)
        return fx.commit_all(wt, "fix add()")
    return f


def make(omg_home, mode):
    from helpers import Harness
    enable(mode)
    h = Harness()
    job = h.submit()["job_id"]
    h.to_approval(job)
    h.approve(job)
    return h, job


def receipts_of(h, job, kind=None):
    q = "SELECT * FROM tool_receipts WHERE job_id=?" + (" AND kind=?" if kind else "") + " ORDER BY created_at, rowid"
    return h.e._all(q, (job, kind) if kind else (job,))


def tasks(h, job, kind):
    return h.e._all("SELECT * FROM tasks WHERE job_id=? AND kind=? ORDER BY created_at", (job, kind))


@need("python-ruff", "python-basedpyright")
def test_type_error_that_unit_tests_miss_rejects_candidate_and_opens_repair(omg_home):
    h, job = make(omg_home, "enforce")
    try:
        _, sha, _ = h.implement(job, fix=fixed(TYPE_ERROR))
        impl = tasks(h, job, "implement")
        assert [t["status"] for t in impl][0] == "rejected", "unit tests pass but the type error is caught"
        assert "language diagnostics" in impl[0]["block_reason"]
        assert h.e._job(job)["repair_cycles"] == 1
        assert len(impl) == 2 and impl[1]["status"] in ("dispatched", "queued", "awaiting_resume")
        assert not tasks(h, job, "code_review"), "no review is spent on a candidate that fails the diagnostics gate"
        verification = h.e._one("SELECT status FROM verifications WHERE job_id=? AND purpose='candidate'", (job,))
        assert verification["status"] == "passed", "the oracle/unit verification itself passed"
        r = receipts_of(h, job, "lsp_candidate")[0]
        assert r["level"] == "engine_executed" and r["status"] == "findings" and r["revision"] == sha
        summ = json.loads(r["summary_json"])
        assert "reportAssignmentType" in summ["codes"] or "reportReturnType" in summ["codes"], summ
        assert "basedpyright" in r["tool_version"]
        art = h.e._one("SELECT * FROM artifacts WHERE id=?", (json.loads(r["artifact_ids_json"])[0],))
        assert art["kind"] == "lsp_result" and art["revision"] == sha
    finally:
        h.close()


@need("python-ruff", "python-basedpyright")
def test_repair_then_clean_candidate_reaches_final_gate_with_lsp_receipts(omg_home):
    h, job = make(omg_home, "enforce")
    try:
        h.implement(job, fix=fixed(TYPE_ERROR))
        _, sha2, _ = h.implement(job, fix=fixed())
        assert h.e._job(job)["repair_cycles"] == 1
        for kind in ("code_review", "qa"):
            h.review(job, kind)
        _, res = h.integrate(job)
        gate = res.get("gate") or h.e._one("SELECT * FROM gate_results WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                                           (job,))
        checks = json.loads(gate["checks_json"]) if not isinstance(gate, dict) else gate["checks"]
        for name in ("lsp_receipt_for_exact_revision", "lsp_delta_clean", "lsp_config_untouched",
                     "lsp_no_new_suppressions"):
            assert checks[name]["ok"], (name, checks[name])
        assert h.e._job(job)["phase"] == "done" and h.e._job(job)["final_result_revision"] == sha2
        final = receipts_of(h, job, "lsp_final")[0]
        assert final["revision"] == sha2 and final["status"] in ("clean", "not_applicable")
        cand = receipts_of(h, job, "lsp_candidate")
        assert [r["status"] for r in cand] == ["findings", "clean"]
    finally:
        h.close()


@need("python-ruff", "python-basedpyright")
def test_final_gate_refuses_without_an_engine_receipt_for_the_exact_revision(omg_home):
    h, job = make(omg_home, "enforce")
    try:
        h.implement(job, fix=fixed())
        for kind in ("code_review", "qa"):
            h.review(job, kind)
        ok, checks, _ = h.e.gate_checks(job)
        assert not ok and not checks["lsp_receipt_for_exact_revision"]["ok"]
        assert not checks["lsp_delta_clean"]["ok"], "absence of a receipt is a failure, never a pass"
        h.e.conn.execute("INSERT INTO tool_receipts(id,job_id,kind,level,status,revision,tool,created_at,summary_json)"
                         " VALUES ('forged',?,'lsp_final','engine_delivered','clean',?,'x','2026-01-01T00:00:00Z',"
                         "'{\"purpose\":\"final\"}')", (job, h.e._job(job)["candidate_revision"]))
        ok, checks, _ = h.e.gate_checks(job)
        assert not checks["lsp_receipt_for_exact_revision"]["ok"], "a non-gate level can never satisfy the gate"
    finally:
        h.close()


@need("python-ruff", "python-basedpyright")
def test_shadow_mode_records_but_does_not_block(omg_home):
    h, job = make(omg_home, "shadow")
    try:
        h.implement(job, fix=fixed(TYPE_ERROR))
        assert tasks(h, job, "implement")[0]["status"] == "passed"
        assert tasks(h, job, "code_review") and h.e._job(job)["repair_cycles"] == 0
        assert receipts_of(h, job, "lsp_candidate")[0]["status"] == "findings"
    finally:
        h.close()


def test_off_mode_has_no_lsp_side_effects(omg_home):
    h, job = make(omg_home, None)
    try:
        h.implement(job, fix=fixed(TYPE_ERROR))
        assert tasks(h, job, "code_review")
        assert receipts_of(h, job) == []
    finally:
        h.close()


def test_unavailable_diagnostics_block_instead_of_passing(omg_home):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    raw = json.loads((Path(__file__).resolve().parents[2] / "config" / "lsp.json").read_text())
    for p in raw["profiles"].values():
        p["argv"] = ["/nonexistent/server", "--stdio"]
    (cfg / "lsp.json").write_text(json.dumps(raw))
    h, job = make(omg_home, "enforce")
    try:
        h.implement(job, fix=fixed())
        impl = tasks(h, job, "implement")[0]
        assert impl["status"] == "blocked" and "unavailable (fail closed)" in impl["block_reason"]
        assert not tasks(h, job, "code_review")
        r = receipts_of(h, job, "lsp_candidate")[0]
        assert r["status"] == "unavailable"
        assert h.e._job(job)["phase"] == "blocked"
    finally:
        h.close()


def test_code_language_without_an_enabled_profile_blocks(omg_home):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    raw = json.loads((Path(__file__).resolve().parents[2] / "config" / "lsp.json").read_text())
    for p in raw["profiles"].values():
        p["enabled"] = False
    (cfg / "lsp.json").write_text(json.dumps(raw))
    h, job = make(omg_home, "enforce")
    try:
        h.implement(job, fix=fixed())
        impl = tasks(h, job, "implement")[0]
        assert impl["status"] == "blocked" and "no enabled language-server profile for python" in impl["block_reason"]
        assert json.loads(receipts_of(h, job, "lsp_candidate")[0]["summary_json"])["uncovered"] == {
            "python": ["calc/__init__.py"]}
    finally:
        h.close()
