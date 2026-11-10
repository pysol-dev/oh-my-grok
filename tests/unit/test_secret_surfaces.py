"""Status / MCP status / export never show webhook URLs or lease/claim tokens — even in a fresh process that never
loaded the hooks file (so no secrets are registered with the redactor)."""
import json

from oh_my_grok import redact as red
from oh_my_grok.engine import Engine
from oh_my_grok.hooks import set_hook
from oh_my_grok.paths import get_paths
from oh_my_grok.transport import HttpResult
from tests.helpers import SECRET_KEY

HOOK_URL = "https://hooks.invalid/routines/trigger/ZZsecretPATHtoken0123456789?sig=QQsecretQUERY987"


def _fail_with_echo(harness):
    set_hook(get_paths(), "explorer", HOOK_URL, SECRET_KEY)
    # transport errors from urllib commonly echo the URL; make the fake one echo URL + key verbatim
    harness.transport.responses = [HttpResult(None, None, f"<urlopen error for {HOOK_URL} key={SECRET_KEY}; redirected to "
                                                   f"https://edge.invalid/routines/trigger/ZZsecretPATHtoken0123456789>")] * 3
    job = harness.submit()["job_id"]
    return job


def _assert_clean(text):
    for needle in ("ZZsecretPATHtoken", "QQsecretQUERY", "routines/trigger", SECRET_KEY):
        assert needle not in text, needle


def test_status_never_shows_webhook_url_even_without_loaded_hooks(harness):
    job = _fail_with_echo(harness)
    row = harness.e._one("SELECT last_error FROM dispatch_outbox WHERE task_id=?",
                         (harness.task(job, "explore")["id"],))
    assert row["last_error"] and "urlopen error" in row["last_error"] and "edge.invalid" in row["last_error"]  # the failure really was recorded
    _assert_clean(row["last_error"])
    # simulate a fresh MCP/CLI process: nothing registered, hooks file never loaded
    red._known.clear()
    fresh = Engine()
    try:
        st = fresh.status(job, include_events=True)
        _assert_clean(json.dumps(st, default=str))
        _assert_clean(json.dumps(red.redact_obj(st), default=str))
        ev = fresh._all("SELECT data_json FROM events WHERE job_id=?", (job,))
        _assert_clean(" ".join(r["data_json"] or "" for r in ev))
    finally:
        fresh.close()


def test_legacy_unredacted_last_error_is_still_hidden_in_status(harness):
    job = harness.submit()["job_id"]
    t = harness.task(job, "explore")
    harness.e.conn.execute("UPDATE dispatch_outbox SET last_error=? WHERE id=?",
                           (f"old error {HOOK_URL}", t["current_dispatch_id"]))
    red._known.clear()
    fresh = Engine()
    try:
        _assert_clean(json.dumps(fresh.status(job), default=str))
    finally:
        fresh.close()


def test_hook_status_is_booleans_only(harness):
    from oh_my_grok.hooks import hook_status
    hs = hook_status(get_paths())
    assert hs and all(isinstance(v, bool) for v in hs.values())


def test_export_redacts_lease_and_claim_tokens(harness, tmp_path):
    job = harness.submit()["job_id"]
    harness.claim(job, "explore")
    t = harness.e._one("SELECT lease_token FROM tasks WHERE job_id=? AND kind='explore'", (job,))
    a = harness.e._one("SELECT claim_token FROM task_attempts WHERE job_id=?", (job,))
    assert t["lease_token"] and a["claim_token"]
    red._known.clear()
    out = harness.e.export(job, tmp_path)
    text = open(out["path"]).read()
    assert t["lease_token"] not in text and a["claim_token"] not in text
    data = json.loads(text)
    assert all(r["claim_token"] == "[REDACTED]" for r in data["task_attempts"])
    assert any(r["lease_token"] == "[REDACTED]" for r in data["tasks"])
    _assert_clean(text)
