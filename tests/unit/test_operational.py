import json

from oh_my_grok import db
from oh_my_grok.operational import evaluate


def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    db.migrate(c)
    return c


def add_job(c, jid="j1", mode="implement", phase="done"):
    c.execute("INSERT INTO jobs(id,idempotency_key,goal,repo_path,mode,requirements_json,requirements_hash,phase,"
              "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (jid, "k" + jid, "g", "/r", mode, "{}", "h", phase, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))


def gate(c, jid="j1", rev="a" * 40):
    c.execute("INSERT INTO gate_results(id,job_id,revision,passed,assurance,checks_json,created_at) "
              "VALUES (?,?,?,1,'x','{}','2026-01-01T00:00:00Z')", ("g" + jid, jid, rev))


def receipt(c, kind, status, level="engine_executed", jid="j1", rev="a" * 40):
    c.execute("INSERT INTO tool_receipts(id,job_id,kind,level,status,revision,tool,created_at) VALUES "
              "(?,?,?,?,?,?,'t','2026-01-01T00:00:00Z')", (f"{kind}{level}{status}", jid, kind, level, status, rev))


def attest(c, kind):
    c.execute("INSERT INTO events(ts,kind,actor,data_json) VALUES ('2026-01-01T00:00:00Z','attestation','u',?)",
              (json.dumps({"attestation": kind}),))


def test_nothing_recorded_is_not_operational(tmp_path):
    assert evaluate(conn(tmp_path))["state"].startswith("NOT OPERATIONAL")


def test_gate_without_lsp_and_git_receipts_does_not_count(tmp_path):
    c = conn(tmp_path)
    add_job(c)
    gate(c)
    assert not evaluate(c)["criteria"]["real_job_gate_with_lsp_and_git_receipts"]["ok"]
    receipt(c, "lsp_final", "clean")
    assert not evaluate(c)["criteria"]["real_job_gate_with_lsp_and_git_receipts"]["ok"]
    receipt(c, "git_history", "ok", level="engine_delivered")
    assert not evaluate(c)["criteria"]["real_job_gate_with_lsp_and_git_receipts"]["ok"], "delivered is not evidence"
    receipt(c, "git_history", "ok")
    assert evaluate(c)["criteria"]["real_job_gate_with_lsp_and_git_receipts"]["ok"]


def test_fixture_jobs_never_count(tmp_path):
    c = conn(tmp_path)
    add_job(c, mode="fixture")
    gate(c)
    receipt(c, "lsp_final", "clean")
    receipt(c, "git_history", "ok")
    assert not evaluate(c)["criteria"]["real_job_gate_with_lsp_and_git_receipts"]["ok"]


def test_only_all_four_criteria_reach_operational(tmp_path):
    c = conn(tmp_path)
    add_job(c)
    gate(c)
    receipt(c, "lsp_final", "clean")
    receipt(c, "git_history", "ok")
    assert evaluate(c)["state"].startswith("PARTIALLY INSTALLED: 1 of 4")
    for k in ("host_cutover", "live_smoke"):
        attest(c, k)
    assert evaluate(c)["state"].startswith("PARTIALLY INSTALLED: 3 of 4")
    attest(c, "live_e2e")
    assert evaluate(c)["state"].startswith("OPERATIONAL")
