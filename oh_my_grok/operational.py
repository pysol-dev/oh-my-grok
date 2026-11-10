"""What can honestly be said about whether this installation works (spec/ADAPTIVE.md section 12).

The verdict is DERIVED from stored evidence, never from a constant or from implemented features:
  a) a passed revision-bound gate for a real (non-fixture) implement job that has engine-executed LSP and git receipts
  b) a recorded human host cutover attestation
  c) recorded human attestations of the two live items from the original spec (smoke, end-to-end)
Anything less is NOT OPERATIONAL (nothing) or PARTIALLY INSTALLED (some). Tests in this repository can satisfy none of
b) or c), and a) only on a disposable repository, so a development branch alone can never claim OPERATIONAL."""
from __future__ import annotations

ATTEST_KINDS = {"host_cutover": "the host MCP registration, routines and bindings were installed and exercised",
                "live_smoke": "original spec 10.20: real POST reached a real routine which claimed via MCP",
                "live_e2e": "original spec 10.21: approved synthetic job across all role stages on real Bots"}


def evaluate(conn) -> dict:
    gate = conn.execute(
        "SELECT g.id, g.job_id, g.revision, g.assurance FROM gate_results g JOIN jobs j ON j.id=g.job_id "
        "WHERE g.passed=1 AND j.mode='implement' AND j.phase='done' AND EXISTS (SELECT 1 FROM tool_receipts r WHERE "
        "r.job_id=g.job_id AND r.kind='lsp_final' AND r.revision=g.revision AND r.level='engine_executed' AND "
        "r.status IN ('clean','not_applicable')) AND EXISTS (SELECT 1 FROM tool_receipts r WHERE r.job_id=g.job_id "
        "AND r.kind='git_history' AND r.revision=g.revision AND r.level='engine_executed' AND r.status='ok') "
        "ORDER BY g.created_at DESC LIMIT 1").fetchone()
    att = {k: conn.execute("SELECT ts, actor, data_json FROM events WHERE kind='attestation' AND "
                           "json_extract(data_json,'$.attestation')=? ORDER BY id DESC LIMIT 1", (k,)).fetchone()
           for k in ATTEST_KINDS}
    criteria = {
        "real_job_gate_with_lsp_and_git_receipts": {
            "ok": gate is not None,
            "detail": f"job {gate['job_id']} revision {gate['revision'][:12]}" if gate else
                      "no passed gate for a done implement-mode job with engine-executed LSP and git receipts"},
        **{k: {"ok": row is not None,
               "detail": f"attested by {row['actor']} at {row['ts']}" if row else f"not attested ({ATTEST_KINDS[k]})"}
           for k, row in att.items()},
    }
    n = sum(1 for c in criteria.values() if c["ok"])
    if n == len(criteria):
        state = "OPERATIONAL (every criterion has recorded evidence; verify the evidence itself)"
    elif n:
        state = "PARTIALLY INSTALLED: " + str(n) + " of " + str(len(criteria)) + " criteria have recorded evidence"
    else:
        state = "NOT OPERATIONAL: no recorded live evidence (see criteria)"
    return {"state": state, "criteria": criteria}
