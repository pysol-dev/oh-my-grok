"""§10 item 17: redaction of bearer tokens, private keys, authorization headers in status/logs/export/terminal."""
import json
import os
import subprocess
from pathlib import Path

from oh_my_grok.redact import redact, redact_obj, register_secret
from oh_my_grok.transport import HttpResult

ROOT = Path(__file__).resolve().parents[2]
PEM = "-----BEGIN PRIVATE KEY-----\nMIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEA\n-----END PRIVATE KEY-----"


def test_patterns():
    s = redact(f"Authorization: Bearer abc.def.ghi123456 token=zzzsecretzzz {PEM} api_key=AKIAXXXXXXXX")
    assert "abc.def.ghi123456" not in s and "zzzsecretzzz" not in s and "MIIBVg" not in s and "AKIAXXXX" not in s


def test_registered_secret_and_dict_keys():
    register_secret("my-registered-secret-value")
    assert "my-registered-secret-value" not in redact("x my-registered-secret-value y")
    o = redact_obj({"authorization": "Bearer q", "nested": [{"key": "v", "ok": "fine"}]})
    assert "q" not in json.dumps(o["authorization"]) and o["nested"][0]["ok"] == "fine"


def test_secret_never_reaches_status_events_export(harness, tmp_path):
    from helpers import SECRET_KEY
    e = harness.e
    harness.transport.responses = [HttpResult(None, None, f"connection reset; Authorization: Bearer {SECRET_KEY}")]
    job = harness.submit()["job_id"]
    harness.do(job, "research", {"sources": ["x"], "api_requirements": [], "risks": []},
               [{"kind": "research", "name": "r.md", "content": "ok"}])
    pkt = harness.claim(job, "explore")
    e.checkpoint(job, pkt["task_id"], pkt["attempt_id"], f"debug: Authorization: Bearer {SECRET_KEY} {PEM}")
    dump = json.dumps(e.status(job, include_events=True)) + json.dumps(e.status())
    dump += json.dumps([dict(r) for r in e._all("SELECT * FROM events")])
    dump += json.dumps([dict(r) for r in e._all("SELECT * FROM dispatch_outbox")])
    dump += json.dumps([dict(r) for r in e._all("SELECT progress_json FROM task_attempts")])
    out = e.export(job, tmp_path / "exp")
    dump += Path(out["path"]).read_text()
    assert SECRET_KEY not in dump and "MIIBVg" not in dump
    # transport bodies never contain the key or the goal
    for c in harness.transport.calls:
        assert set(c["body"]) == {"schema_version", "job_id", "task_id", "dispatch_id", "attempt"}


def test_cli_terminal_output_redacted(harness):
    from helpers import SECRET_KEY
    job = harness.submit()["job_id"]
    env = dict(os.environ)
    outs = ""
    for args in (["status", "--json"], ["status", job], ["hook", "list"], ["hook", "check"], ["roles", "list"],
                 ["doctor", "--json"]):
        r = subprocess.run([str(ROOT / "omg"), *args], capture_output=True, text=True, env=env, timeout=60)
        outs += r.stdout + r.stderr
    assert SECRET_KEY not in outs and "hooks.invalid" not in outs
