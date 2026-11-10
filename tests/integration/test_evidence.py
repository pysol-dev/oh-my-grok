"""Evidence that is not self-report (R9, R10, R11): Context7 receipts made by the engine, citations bound to them,
agent claims that never satisfy a gate, and git history shape. A local MCP server speaks the real protocol; only the
agents are simulated."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oh_my_grok import fixture as fx
from oh_my_grok import gitops, skills
from oh_my_grok.errors import GateRefused, ValidationError

FAKE = str(Path(__file__).resolve().parents[1] / "unit" / "fake_context7.py")
QUOTE = "Widget.configure(retries=3) sets the retry count."


@pytest.fixture
def c7(tmp_path):
    procs = []

    def start(mode="ok"):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        p = subprocess.Popen([sys.executable, FAKE, str(port)], env={**os.environ, "FAKE_C7_MODE": mode},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return f"http://127.0.0.1:{port}/mcp"
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("fake context7 did not start")

    yield start
    for p in procs:
        p.kill()
        p.wait()


def configure(features, url=None):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol["features"] = features
    if url:
        pol["context7_url"] = url
    (cfg / "policy.json").write_text(json.dumps(pol))


def mkh(features, url=None):
    from helpers import Harness
    configure(features, url)
    return Harness()


def explore(h, job):
    h.do(job, "explore", {"repo_map": "m", "files": ["calc/__init__.py:1"], "tests": [], "constraints": [],
                          "questions": []}, [{"kind": "explore", "name": "e.md", "content": "EXPLORE"}])


def research_result(citations=None, **extra):
    r = {"sources": ["https://example.invalid/widgets"], "api_requirements": ["Widget.configure"], "risks": [],
         **extra}
    if citations is not None:
        r["citations"] = citations
    return r


def finish_research(h, job, citations):
    pkt = h.claim(job, "research")
    return h.e.complete(job, pkt["task_id"], pkt["attempt_id"], research_result(citations),
                        [{"kind": "research", "name": "r.md", "content": "LIBRARIAN"}])


def receipts_of(h, job, kind):
    return h.e._all("SELECT * FROM tool_receipts WHERE job_id=? AND kind=? ORDER BY created_at, rowid", (job, kind))


def new_job(h):
    job = h.submit()["job_id"]
    explore(h, job)
    return job


# ----------------------------------------------------------------------------------------- Context7 receipts
def test_docs_refused_when_feature_off(omg_home):
    h = mkh({})
    try:
        job = h.submit()["job_id"]
        with pytest.raises(GateRefused):
            h.e.docs(job, "retries", library_name="widget")
        assert receipts_of(h, job, "context7_query") == []
    finally:
        h.close()


def test_engine_fetch_creates_executed_receipt_with_stored_transcript(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        job = new_job(h)
        r = h.e.docs(job, "how do retries work", library_name="widget")
        assert r["ok"] and QUOTE in r["text"] and r["server"]["name"] == "FakeContext7"
        row = receipts_of(h, job, "context7_query")[0]
        assert row["id"] == r["receipt_id"] and row["level"] == "engine_executed" and row["status"] == "ok"
        assert row["output_sha256"] == r["response_sha256"] and row["tool_version"] == "FakeContext7@9.9.9"
        art = h.e._one("SELECT * FROM artifacts WHERE id=?", (json.loads(row["artifact_ids_json"])[0],))
        stored = json.loads((h.e._art_root(job) / art["rel_path"]).read_text())
        assert [c["tool"] for c in stored["calls"]] == ["resolve-library-id", "query-docs"]
        assert stored["calls"][-1]["response"] == r["text"]
    finally:
        h.close()


def test_failed_fetch_returns_error_and_a_receipt_that_cannot_be_cited(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7("error_query"))
    try:
        job = new_job(h)
        r = h.e.docs(job, "q", library_name="widget")
        assert r["ok"] is False and r["error_kind"] == "tool_error" and "text" not in r
        row = receipts_of(h, job, "context7_query")[0]
        assert row["status"] == "failed"
        with pytest.raises(ValidationError, match="not a successful"):
            finish_research(h, job, [{"receipt_id": row["id"], "quote": QUOTE}])
    finally:
        h.close()


def test_unreachable_endpoint_fails_without_evidence(omg_home):
    h = mkh({"docs_evidence": "enforce"}, "http://127.0.0.1:9/mcp")
    try:
        job = new_job(h)
        r = h.e.docs(job, "q", library_name="widget")
        assert r["ok"] is False and r["error_kind"] == "connect"
    finally:
        h.close()


def test_docs_budget_per_job(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        job = new_job(h)
        h.e.policy["docs_max_per_job"] = 1
        h.e.docs(job, "q", library_name="widget")
        with pytest.raises(GateRefused, match="budget"):
            h.e.docs(job, "q", library_name="widget")
    finally:
        h.close()


# ----------------------------------------------------------------------------------------- citations
def test_valid_citation_is_reverified_and_research_passes(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        job = new_job(h)
        r = h.e.docs(job, "retries", library_name="widget")
        finish_research(h, job, [{"receipt_id": r["receipt_id"], "quote": "Widget.configure(retries=3)   sets the\nretry count."}])
        assert h.task(job, "research")["status"] == "passed"
        cit = receipts_of(h, job, "citation")[0]
        assert cit["level"] == "engine_reverified" and json.loads(cit["summary_json"])["receipt_id"] == r["receipt_id"]
    finally:
        h.close()


@pytest.mark.parametrize("why,citations", [
    ("no citations at all (agent prose only)", None),
    ("empty citation list", []),
    ("fabricated receipt id", [{"receipt_id": "00000000-0000-0000-0000-000000000000", "quote": QUOTE}]),
    ("unknown keys", "REAL+extra"),
    ("fabricated quote", "REAL+fake quote"),
    ("quote too short", "REAL+short"),
])
def test_missing_or_fabricated_evidence_fails_under_enforce(omg_home, c7, why, citations):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        job = new_job(h)
        r = h.e.docs(job, "retries", library_name="widget")
        if citations == "REAL+extra":
            citations = [{"receipt_id": r["receipt_id"], "quote": QUOTE, "tool_ran": True}]
        elif citations == "REAL+fake quote":
            citations = [{"receipt_id": r["receipt_id"], "quote": "Widget.configure(retries=99) disables retries."}]
        elif citations == "REAL+short":
            citations = [{"receipt_id": r["receipt_id"], "quote": "retries"}]
        with pytest.raises(ValidationError):
            finish_research(h, job, citations)
        assert h.task(job, "research")["status"] == "working", why
        assert receipts_of(h, job, "citation") == []
    finally:
        h.close()


def test_receipt_from_another_job_cannot_be_cited(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        other = new_job(h)
        r = h.e.docs(other, "retries", library_name="widget")
        h2 = h.submit(key="second-job-key-01")["job_id"]
        explore(h, h2)
        with pytest.raises(ValidationError, match="this job"):
            finish_research(h, h2, [{"receipt_id": r["receipt_id"], "quote": QUOTE}])
    finally:
        h.close()


def test_forged_lower_level_receipt_cannot_be_cited(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        job = new_job(h)
        r = h.e.docs(job, "retries", library_name="widget")
        h.e.conn.execute("UPDATE tool_receipts SET level='engine_delivered' WHERE id=?", (r["receipt_id"],))
        with pytest.raises(ValidationError, match="not a successful"):
            finish_research(h, job, [{"receipt_id": r["receipt_id"], "quote": QUOTE}])
    finally:
        h.close()


def test_tampered_stored_transcript_cannot_back_a_citation(omg_home, c7):
    h = mkh({"docs_evidence": "enforce"}, c7())
    try:
        job = new_job(h)
        r = h.e.docs(job, "retries", library_name="widget")
        row = receipts_of(h, job, "context7_query")[0]
        art = h.e._one("SELECT * FROM artifacts WHERE id=?", (json.loads(row["artifact_ids_json"])[0],))
        p = h.e._art_root(job) / art["rel_path"]
        data = json.loads(p.read_text())
        data["calls"][-1]["response"] += " Widget.configure(retries=99) disables retries entirely."
        p.write_text(json.dumps(data))
        with pytest.raises(ValidationError, match="integrity"):
            finish_research(h, job, [{"receipt_id": r["receipt_id"], "quote": "disables retries entirely."}])
    finally:
        h.close()


def test_shadow_mode_records_invalid_citations_without_blocking(omg_home, c7):
    h = mkh({"docs_evidence": "shadow"}, c7())
    try:
        job = new_job(h)
        finish_research(h, job, [{"receipt_id": "nope", "quote": QUOTE}])
        assert h.task(job, "research")["status"] == "passed"
        ev = h.e._one("SELECT data_json FROM events WHERE job_id=? AND kind='citations_checked'", (job,))
        assert json.loads(ev["data_json"])["invalid"] == 1
        assert receipts_of(h, job, "citation") == []
    finally:
        h.close()


def test_off_mode_ignores_citations_entirely(omg_home):
    h = mkh({})
    try:
        job = new_job(h)
        finish_research(h, job, [{"receipt_id": "nope", "quote": QUOTE}])
        assert h.task(job, "research")["status"] == "passed"
        assert receipts_of(h, job, "citation") == []
    finally:
        h.close()


def test_not_applicable_research_refused_for_external_api_jobs(omg_home):
    from helpers import Harness
    configure({"docs_evidence": "enforce", "adaptive_roster": "enforce"})
    h = Harness()
    try:
        scope = {"files_estimate": 2, "new_dependency": False, "external_api": True, "security_sensitive": False,
                 "cross_cutting": False}
        job = h.submit(intent="implementation", scope=scope)["job_id"]
        explore(h, job)
        pkt = h.claim(job, "research")
        with pytest.raises(ValidationError, match="external API"):
            h.e.complete(job, pkt["task_id"], pkt["attempt_id"], {"not_applicable": True, "reason": "none needed"})
    finally:
        h.close()


# ----------------------------------------------------------------------------------------- git discipline
def trailer_fix(job, trailer=True, extra_commits=0):
    def f(wt):
        p = wt / "calc" / "__init__.py"
        p.write_text(p.read_text().replace(fx.FIX_FROM, fx.FIX_TO).replace(fx.FAKE_LINES, ""))
        msg = "fix add()" + (f"\n\nOmg-Job: {job}" if trailer else "")
        sha = fx.commit_all(wt, msg)
        for i in range(extra_commits):
            p.write_text(p.read_text() + f"# note {i}\n")
            sha = fx.commit_all(wt, f"note {i}\n\nOmg-Job: {job}")
        return sha
    return f


def run_to_gate(h, job, fix):
    h.to_approval(job)
    h.approve(job)
    pkt = h.claim(job, "implement")
    sha = fix(Path(pkt["worktree_root"]))
    h.e.complete(job, pkt["task_id"], pkt["attempt_id"], {"commit_sha": sha, "touched_files": ["calc/__init__.py"],
                                                          "summary": "fix", "unit_evidence": "ran", "deviations": [],
                                                          "risks": []})
    for kind in ("code_review", "qa"):
        h.review(job, kind)
    h.integrate(job)
    return h.e._one("SELECT * FROM gate_results WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job,))


def test_packet_carries_skill_pack_and_delivery_is_recorded_as_delivered_only(omg_home):
    h = mkh({"git_discipline": "enforce"})
    try:
        job = h.submit()["job_id"]
        h.to_approval(job)
        h.approve(job)
        pkt = h.claim(job, "implement")
        pack = pkt["skill_pack"]
        assert [s["name"] for s in pack] == ["git-master"] and pkt["commit_trailer"] == f"Omg-Job: {job}"
        assert pack[0]["sha256"] == skills.load("git-master")["sha256"]
        row = receipts_of(h, job, "skill_pack")[0]
        assert row["level"] == "engine_delivered" and row["output_sha256"] == skills.pack_hash(pack)
        assert "not that it was read" in row["summary_json"]
    finally:
        h.close()


def test_off_mode_packet_has_no_skill_pack(omg_home):
    h = mkh({})
    try:
        job = h.submit()["job_id"]
        h.to_approval(job)
        h.approve(job)
        pkt = h.claim(job, "implement")
        assert "skill_pack" not in pkt and receipts_of(h, job, "skill_pack") == []
    finally:
        h.close()


def test_conforming_history_passes_final_gate_and_receipt_is_executed(omg_home):
    h = mkh({"git_discipline": "enforce"})
    try:
        job = h.submit()["job_id"]
        gate = run_to_gate(h, job, trailer_fix(job))
        checks = json.loads(gate["checks_json"])
        assert gate["passed"] == 1, {k: v for k, v in checks.items() if not v["ok"]}
        for k in ("git_history_linear", "git_commit_count_within_budget", "git_commits_carry_job_trailer"):
            assert checks[k]["ok"]
        assert receipts_of(h, job, "git_history")[0]["level"] == "engine_executed"
    finally:
        h.close()


def test_missing_trailer_fails_final_gate(omg_home):
    h = mkh({"git_discipline": "enforce"})
    try:
        job = h.submit()["job_id"]
        gate = run_to_gate(h, job, trailer_fix(job, trailer=False))
        checks = json.loads(gate["checks_json"])
        assert gate["passed"] == 0 and not checks["git_commits_carry_job_trailer"]["ok"]
        assert h.e._job(job)["phase"] != "done"
    finally:
        h.close()


def test_too_many_commits_fails_final_gate(omg_home):
    h = mkh({"git_discipline": "enforce"})
    try:
        h.e.policy["git_max_commits"] = 2
        job = h.submit()["job_id"]
        gate = run_to_gate(h, job, trailer_fix(job, extra_commits=2))
        checks = json.loads(gate["checks_json"])
        assert gate["passed"] == 0 and not checks["git_commit_count_within_budget"]["ok"]
    finally:
        h.close()


def test_rewritten_repair_is_refused_and_fails_the_history_gate(omg_home):
    """A repair that resets to the base and re-commits does not descend from the rejected candidate.
    40d2645 accepted it and the final gate passed."""
    h = mkh({"git_discipline": "enforce"})
    try:
        job = h.submit()["job_id"]
        h.to_approval(job)
        h.approve(job)
        _, sha1, _ = h.implement(job, fix=trailer_fix(job))
        h.review(job, "code_review", verdict="reject")
        pkt = h.claim(job, "implement")
        wt = Path(pkt["worktree_root"])
        base = h.e._job(job)["base_revision"]
        subprocess.run(["git", "-C", str(wt), "reset", "--hard", base], check=True, capture_output=True)
        assert gitops.head(wt) == base

        def rewrite(path):
            src = path / "calc" / "__init__.py"
            src.write_text(src.read_text().replace(fx.FIX_FROM, fx.FIX_TO) + "\n# rewritten history\n")
            return fx.commit_all(path, f"rewritten\n\nOmg-Job: {job}")

        sha2 = rewrite(wt)
        assert sha2 != sha1 and gitops.is_ancestor(wt, sha1, sha2) is False
        with pytest.raises(ValidationError, match="git_history_not_descendant"):
            h.e.complete(job, pkt["task_id"], pkt["attempt_id"],
                         {"commit_sha": sha2, "touched_files": ["calc/__init__.py"], "summary": "rewritten",
                          "unit_evidence": "ran", "deviations": [], "risks": []})
        assert h.e._job(job)["candidate_revision"] == sha1
        checks: dict = {}

        def chk(name, ok, detail=""):
            checks[name] = {"ok": bool(ok), "detail": detail}

        h.e._history_gate_checks(job, sha2, chk)
        assert checks["git_history_not_descendant"]["ok"] is False
        assert sha1[:12] in checks["git_history_not_descendant"]["detail"]
    finally:
        h.close()


def test_repair_that_extends_the_rejected_candidate_passes_the_gate(omg_home):
    h = mkh({"git_discipline": "enforce"})
    try:
        job = h.submit()["job_id"]
        h.to_approval(job)
        h.approve(job)
        h.implement(job, fix=trailer_fix(job))
        h.review(job, "code_review", verdict="reject")

        def extend(wt):
            p = wt / "calc" / "__init__.py"
            p.write_text(p.read_text() + "# repair on top\n")
            return fx.commit_all(wt, f"repair on top\n\nOmg-Job: {job}")

        h.implement(job, fix=extend)
        h.review(job, "code_review")
        h.review(job, "qa")
        _, res = h.integrate(job)
        assert res["final_gate"]["passed"] is True, res["final_gate"]["checks"]
        assert res["final_gate"]["checks"]["git_history_not_descendant"]["ok"] is True
    finally:
        h.close()


def test_shadow_records_history_but_does_not_gate(omg_home):
    h = mkh({"git_discipline": "shadow"})
    try:
        job = h.submit()["job_id"]
        gate = run_to_gate(h, job, trailer_fix(job, trailer=False))
        assert gate["passed"] == 1
        assert receipts_of(h, job, "git_history")[0]["status"] == "violations"
    finally:
        h.close()


def test_forged_lower_level_history_receipt_does_not_satisfy_gate(omg_home):
    h = mkh({"git_discipline": "enforce"})
    try:
        job = h.submit()["job_id"]
        h.to_approval(job)
        h.approve(job)
        h.implement(job, fix=trailer_fix(job))
        sha = h.e._job(job)["candidate_revision"]
        h.e.conn.execute("INSERT INTO tool_receipts(id,job_id,kind,level,status,revision,tool,created_at,summary_json)"
                         " VALUES ('f1',?,'git_history','engine_delivered','ok',?,'git','2026-01-01T00:00:00Z',"
                         "'{\"commits\":1,\"max_commits\":50}')", (job, sha))
        ok, checks, _ = h.e.gate_checks(job)
        assert not checks["git_history_receipt_for_exact_revision"]["ok"]
    finally:
        h.close()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()

    def g(*a):
        return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@e.org")
    g("config", "user.name", "t")
    (r / "a").write_text("1")
    g("add", "-A")
    g("commit", "-qm", "base")
    return r, g


def test_history_shape_detects_merge_commits(repo):
    r, g = repo
    base = g("rev-parse", "HEAD")
    g("checkout", "-qb", "side")
    (r / "s").write_text("s")
    g("add", "-A")
    g("commit", "-qm", "side\n\nOmg-Job: J1")
    g("checkout", "-q", "main")
    (r / "m").write_text("m")
    g("add", "-A")
    g("commit", "-qm", "main\n\nOmg-Job: J1")
    g("merge", "-q", "--no-ff", "-m", "merge\n\nOmg-Job: J1", "side")
    shape = gitops.history_shape(r, base, g("rev-parse", "HEAD"), "J1", 50)
    assert shape["merge_commits"] and not shape["ok"]


def test_history_shape_trailer_must_name_this_job(repo):
    r, g = repo
    base = g("rev-parse", "HEAD")
    (r / "x").write_text("x")
    g("add", "-A")
    g("commit", "-qm", "x\n\nOmg-Job: OTHER")
    assert gitops.history_shape(r, base, g("rev-parse", "HEAD"), "J1", 50)["missing_trailer"]
    assert gitops.history_shape(r, base, g("rev-parse", "HEAD"), "OTHER", 50)["ok"]


def test_history_shape_accepts_legacy_omo_job_trailer(repo):
    r, g = repo
    base = g("rev-parse", "HEAD")
    (r / "y").write_text("y")
    g("add", "-A")
    g("commit", "-qm", "y\n\nOmo-Job: J1")
    assert gitops.history_shape(r, base, g("rev-parse", "HEAD"), "J1", 50)["ok"]
