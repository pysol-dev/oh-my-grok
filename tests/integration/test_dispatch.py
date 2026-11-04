"""T04 / §10 items 7-8: outbox dispatcher against a REAL local HTTP server (loopback mock of the routine
webhook endpoint; not a live Grok Bot), bounded retries, rate limits, re-wake, restart survival."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omo_grok.engine import Engine
from omo_grok.hooks import set_hook
from omo_grok.paths import get_paths
from omo_grok.transport import HttpResult, post_json

KEY = "whsec_LOCALTEST_0123456789abcdef"


class Hook:
    def __init__(self):
        self.requests = []
        self.plan = []  # list of (status, headers) or "hang"
        hook = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                hook.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "ctype": self.headers.get("Content-Type"), "body": json.loads(body)})
                status, hdrs = hook.plan.pop(0) if hook.plan else (200, {})
                if status == "hang":
                    import time
                    time.sleep(3)
                    status = 200
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def live_hook(omo_home, monkeypatch):
    monkeypatch.setenv("OMO_ALLOW_INSECURE_HOOKS", "1")
    h = Hook()
    yield h
    h.close()


@pytest.fixture
def h(live_hook):
    from helpers import Harness
    hn = Harness()
    hn.e.transport = post_json  # real HTTP client for this module
    hn.e.policy["http_read_timeout_seconds"] = 1
    for role in ("explorer", "librarian", "metis", "momus", "executor", "code-reviewer", "qa-executor"):
        set_hook(get_paths(), role, f"{live_hook.url}/{role}", KEY)
    hn.hook = live_hook
    yield hn
    hn.close()


def test_fixed_body_bearer_and_200_is_not_completion(h):
    job = h.submit()["job_id"]
    reqs = h.hook.requests
    assert {r["path"] for r in reqs} == {"/explorer", "/librarian"}
    for r in reqs:
        assert r["auth"] == f"Bearer {KEY}" and r["ctype"].startswith("application/json")
        assert set(r["body"]) == {"schema_version", "job_id", "task_id", "dispatch_id", "attempt"}
        assert r["body"]["schema_version"] == 1 and r["body"]["job_id"] == job and r["body"]["attempt"] == 1
    t = h.task(job, "explore")
    d = h.e._one("SELECT * FROM dispatch_outbox WHERE id=?", (t["current_dispatch_id"],))
    assert d["status"] == "accepted" and t["status"] == "dispatched"  # accepted, NOT claimed/completed
    assert h.e.status(job)["phase"] == "research"


def test_5xx_and_network_errors_retry_with_capped_backoff(h):
    h.hook.plan = [(500, {}), (503, {})] + [(502, {})] * 0
    job = h.submit()["job_id"]
    rows = h.e._all("SELECT * FROM dispatch_outbox WHERE job_id=?", (job,))
    assert sorted(r["status"] for r in rows) == ["retry_wait", "retry_wait"]
    assert all(r["next_attempt_at"] > h.clock() for r in rows)
    assert h.e.flush_outbox()["sent"] == 0  # not due yet: no tight retry loop
    h.clock.advance(1000)
    st = h.e.flush_outbox()
    assert st["accepted"] == 2
    # backoff values are capped by policy
    assert h.e._backoff(50, None) == h.e.policy["backoff_cap_seconds"]


def test_429_retry_after_honoured_and_hourly_cap(h):
    h.hook.plan = [(429, {"Retry-After": "90"})]
    job = h.submit()["job_id"]
    r = [x for x in h.e._all("SELECT * FROM dispatch_outbox WHERE job_id=?", (job,)) if x["status"] == "retry_wait"]
    assert len(r) == 1 and r[0]["next_attempt_at"] - h.clock() >= 90 and "429" in r[0]["last_error"]
    # local hourly per-Bot cap: never flood
    h.e.policy["per_bot_hourly_dispatch_limit"] = 1
    for i in range(3):
        h.submit()
    sent_explorer = [q for q in h.hook.requests if q["path"] == "/explorer"]
    assert len(sent_explorer) == 1
    ev = h.e._all("SELECT * FROM events WHERE kind='dispatch_rate_limited'")
    assert ev


def test_paused_routine_404_and_bad_key_401_block_with_diagnostics(h):
    errs = ""
    for code in (404, 401):
        h.hook.plan = [(code, {})]
        job = h.submit()["job_id"]
        s = h.e.status(job)
        errs += " ".join(t["block_reason"] or "" for t in s["tasks"])
        assert s["phase"] == "blocked" and KEY not in json.dumps(s)
        n = len(h.hook.requests)
        h.clock.advance(100_000)
        h.e.reconcile()
        assert len(h.hook.requests) == n  # permanent failure: no retries
    assert "routine missing, paused or disabled" in errs and "webhook key rejected" in errs


def test_network_failure_is_uncertain_and_retried(h, omo_home):
    set_hook(get_paths(), "explorer", "http://127.0.0.1:9/explorer", KEY)  # closed port
    job = h.submit()["job_id"]
    t = h.task(job, "explore")
    d = h.e._one("SELECT * FROM dispatch_outbox WHERE id=?", (t["current_dispatch_id"],))
    assert d["status"] == "retry_wait" and d["http_status"] is None


def test_retries_are_bounded_then_blocked(h):
    h.e.policy["dispatch_max_retries"] = 2
    h.hook.plan = [(500, {})] * 20
    job = h.submit(key="bounded-0001")["job_id"]
    for _ in range(6):
        h.clock.advance(5000)
        h.e.flush_outbox()
    t = h.task(job, "explore")
    d = h.e._one("SELECT * FROM dispatch_outbox WHERE id=?", (t["current_dispatch_id"],))
    assert d["status"] == "failed" and t["status"] == "blocked"
    assert len([q for q in h.hook.requests if q["path"] == "/explorer"]) == 3  # 1 + 2 retries


def test_item8_reconcile_rewakes_unclaimed_and_is_bounded(h):
    e = h.e
    job = h.submit()["job_id"]
    first = h.task(job, "explore")["current_dispatch_id"]
    assert e.reconcile(job)["rewakes"] == 0  # too early
    h.clock.advance(e.policy["rewake_after_seconds"] + 1)
    rep = e.reconcile(job)
    assert rep["rewakes"] == 2
    t = h.task(job, "explore")
    assert t["current_dispatch_id"] != first and t["status"] == "dispatched"
    bodies = [q["body"] for q in h.hook.requests if q["path"] == "/explorer"]
    assert len(bodies) == 2 and bodies[1]["attempt"] == 1 and bodies[1]["dispatch_id"] != first
    with pytest.raises(Exception):  # old dispatch id can no longer claim
        e.claim(job, t["id"], first)
    for _ in range(10):  # bounded: no flooding
        h.clock.advance(e.policy["rewake_after_seconds"] + 1)
        e.reconcile(job)
    n = len([q for q in h.hook.requests if q["path"] == "/explorer"])
    assert n == 1 + e.policy["max_rewakes"]
    assert h.task(job, "explore")["status"] == "blocked"
    assert "never claimed" in h.task(job, "explore")["block_reason"]


def test_item8_recovery_survives_process_restart(h):
    e = h.e
    h.hook.plan = [(503, {}), (503, {})]
    job = h.submit()["job_id"]
    e.close()
    # a brand new engine (fresh process state) picks up persisted backoff and dispatches
    e2 = Engine(transport=post_json, clock=h.clock)
    h.clock.advance(5000)
    st = e2.reconcile()
    assert st["dispatch"]["accepted"] == 2
    assert all(t["dispatch"]["status"] == "accepted" for t in e2.status(job)["tasks"] if t["dispatch"])
    h.e = e2


def test_interrupted_send_is_recovered_as_uncertain(h):
    e = h.e
    e.policy["inline_dispatch"] = False
    job = h.submit()["job_id"]
    from omo_grok import db
    with db.tx(e.conn):  # simulate crash after marking 'sending' but before recording the result
        e.conn.execute("UPDATE dispatch_outbox SET status='sending', lease_until=? WHERE job_id=?",
                       (h.clock() - 1, job))
    rep = e.reconcile(job)
    assert rep["uncertain_sends"] == 2 and rep["dispatch"]["accepted"] == 2


def test_claim_supersedes_pending_duplicates_duplicate_webhook_harmless(h):
    e = h.e
    job = h.submit()["job_id"]
    h.clock.advance(e.policy["rewake_after_seconds"] + 1)
    e.policy["inline_dispatch"] = False
    e.reconcile(job, flush=False)  # new dispatch published but not yet sent
    t = h.task(job, "explore")
    e.claim(job, t["id"], t["current_dispatch_id"])
    st = e.flush_outbox()
    assert st["accepted"] in (0, 1)  # librarian may send; explorer's pending duplicate is superseded
    assert all(q["path"] != "/explorer" for q in h.hook.requests[2:])


def test_per_bot_serialization(h):
    e = h.e
    e.bind_role("explorer", "dbbe6828-c2fb-4474-8fe3-a0e6df942fc6", "Explorer", routine_id="routine-explorer",
                independence_group="g-explorer", max_concurrent=1, verified=True, provenance="test")
    a = h.submit()["job_id"]
    b = h.submit()["job_id"]
    tb = h.task(b, "explore")
    assert h.task(a, "explore")["status"] == "dispatched"
    assert tb["status"] == "queued" and tb["block_reason"].startswith("bot_busy")
    h.do(a, "explore", {"repo_map": "x", "files": [], "tests": [], "constraints": [], "questions": []},
         [{"kind": "explore", "name": "e.md", "content": "x"}])
    assert h.task(b, "explore")["status"] == "dispatched"


def test_unbound_or_unverified_role_waits_with_reason(omo_home):
    from helpers import Harness
    hn = Harness(bind=False)
    job = hn.submit()["job_id"]
    s = hn.e.status(job)
    assert all(t["status"] == "queued" for t in s["tasks"] if t["kind"] in ("explore", "research"))
    assert "no enabled role binding" in s["next_action"]
    assert hn.transport.calls == []
    hn.close()


def test_transport_unit_timeouts():
    r = post_json("http://10.255.255.1:81/x", KEY, {"a": 1}, 0.5, 0.5)
    assert r.status is None and KEY not in (r.error or "")


def test_mock_transport_result_types():
    assert HttpResult(200, None, None).status == 200
