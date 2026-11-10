"""Live Context7 through the engine (opt-in: OMO_LIVE_CONTEXT7=1). Produces docs/evidence/context7-live-*.json:
the engine's own receipt, the stored transcript hash, and a citation the engine re-verified against that transcript.
This proves the connector works against the real service from this machine; it says nothing about the live host."""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from test_evidence import explore, finish_research, mkh, receipts_of

pytestmark = pytest.mark.skipif(os.environ.get("OMO_LIVE_CONTEXT7") != "1", reason="set OMO_LIVE_CONTEXT7=1")
EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "evidence"


def test_live_receipt_and_verified_citation(omo_home):
    h = mkh({"docs_evidence": "enforce"})
    try:
        job = h.submit()["job_id"]
        explore(h, job)
        r = h.e.docs(job, "how to define and use fixtures", library_name="pytest")
        assert r["ok"], r
        assert r["server"]["name"] == "Context7" and r["library_id"].startswith("/")
        lines = [l.strip() for l in r["text"].splitlines() if len(l.strip()) >= 40 and not l.startswith("Source")]
        quote = lines[0][:200]
        finish_research(h, job, [{"receipt_id": r["receipt_id"], "quote": quote}])
        assert h.task(job, "research")["status"] == "passed"
        rc = receipts_of(h, job, "context7_query")[0]
        cit = receipts_of(h, job, "citation")[0]
        art = h.e._one("SELECT * FROM artifacts WHERE id=?", (json.loads(rc["artifact_ids_json"])[0],))
        assert hashlib.sha256((h.e._art_root(job) / art["rel_path"]).read_bytes()).hexdigest() == art["sha256"]
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        out = EVIDENCE / f"context7-live-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        out.write_text(json.dumps({
            "what": "engine-executed live Context7 query and engine-reverified citation (omo-grok test_context7_live)",
            "endpoint": json.loads(rc["summary_json"])["endpoint"], "server": r["server"],
            "protocol_version": json.loads(rc["summary_json"])["protocol_version"],
            "authenticated": json.loads(rc["summary_json"])["authenticated"],
            "library_id": r["library_id"], "query": "how to define and use fixtures",
            "receipt": {"level": rc["level"], "status": rc["status"], "tool_version": rc["tool_version"],
                        "input_sha256": rc["input_sha256"], "response_sha256": rc["output_sha256"],
                        "transcript_artifact_sha256": art["sha256"]},
            "citation_receipt": {"level": cit["level"], "status": cit["status"], "quote_sha256": cit["input_sha256"],
                                 "quote_excerpt": quote},
            "limits": ["a live response proves the service answered now, not that any bot called it",
                       "anonymous rate limits and key behaviour are not characterised"],
        }, indent=2, sort_keys=True) + "\n")
    finally:
        h.close()
