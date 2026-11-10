"""Test harness: simulated agents act ONLY through the public Engine API (same calls the MCP tools make).
Network is mocked via an injected transport; these are NOT live proofs."""
from __future__ import annotations

import uuid
from pathlib import Path

from oh_my_grok import fixture as fx
from oh_my_grok.engine import Engine
from oh_my_grok.hooks import set_hook
from oh_my_grok.paths import get_paths
from oh_my_grok.transport import HttpResult

BOTS = {
    "hephaestus": ("f497e4a7-5d7d-46e4-829f-b0da987cbac4", "Hephaestus", "g-hephaestus"),
    "explorer": ("dbbe6828-c2fb-4474-8fe3-a0e6df942fc6", "Explorer", "g-explorer"),
    "librarian": ("78a94f3f-cb51-473f-b9d4-9a06fc025f59", "Librarian", "g-librarian"),
    "metis": ("41b28a72-3ece-45b9-9008-000f3fce4da7", "Metis", "g-metis"),
    "momus": ("42b0d03a-d1fb-4fdc-87b1-35d9123f6f74", "Momus", "g-momus"),
    "executor": ("e3dfae0e-9d90-4358-8e90-2e4c6734b092", "Executor", "g-executor"),
    "code-reviewer": ("42b0d03a-d1fb-4fdc-87b1-35d9123f6f74", "Momus", "g-momus"),
    "qa-executor": ("41b28a72-3ece-45b9-9008-000f3fce4da7", "Metis", "g-metis"),
}
SECRET_KEY = "sk-test-SUPERSECRET-0123456789abcdef"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []  # queue of HttpResult; default 200

    def __call__(self, url, key, body, ct, rt):
        self.calls.append({"url": url, "key": key, "body": body, "timeouts": (ct, rt)})
        if self.responses:
            return self.responses.pop(0)
        return HttpResult(200, None, None)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Harness:
    def __init__(self, bind=True, executor_bot=None):
        self.transport = FakeTransport()
        self.clock = Clock()
        self.e = Engine(transport=self.transport, clock=self.clock)
        if bind:
            self.bind_all(executor_bot=executor_bot)

    def close(self):
        self.e.close()

    def bind_all(self, executor_bot=None, skip=()):
        paths = get_paths()
        for role, (bid, name, grp) in BOTS.items():
            if role == "hephaestus" or role in skip:
                continue  # no resume routine: Hephaestus work goes to awaiting_resume
            if role == "executor" and executor_bot:
                bid, name, grp = executor_bot
            self.e.bind_role(role, bid, name, routine_id=f"routine-{role}", routine_name=f"omg-bus-{role}",
                             independence_group=grp, max_concurrent=4, verified=True, provenance="test")
            set_hook(paths, role, f"https://hooks.invalid/{role}", SECRET_KEY)
            self.e.mark_hook_configured(role, True)

    # ------------------------------------------------------------------ helpers
    def task(self, job, kind, status=None):
        q = "SELECT * FROM tasks WHERE job_id=? AND kind=?"
        a = [job, kind]
        if status:
            q += " AND status=?"
            a.append(status)
        return self.e._one(q + " ORDER BY created_at DESC LIMIT 1", a)

    def claim(self, job, kind):
        t = self.task(job, kind)
        assert t["status"] in ("dispatched", "awaiting_resume"), (kind, t["status"], t["block_reason"])
        return self.e.claim(job, t["id"], t["current_dispatch_id"])

    def do(self, job, kind, result, refs=None):
        pkt = self.claim(job, kind)
        return pkt, self.e.complete(job, pkt["task_id"], pkt["attempt_id"], result, refs)

    def submit(self, mode="fixture", key=None, **kw):
        return self.e.submit("Fix calc.add in the synthetic fixture", key or f"k-{uuid.uuid4().hex}", mode=mode,
                             acceptance_criteria=["unittest suite exits 0"], **kw)

    def research(self, job):
        self.do(job, "explore", {"repo_map": "calc/__init__.py; tests/test_calc.py", "files": ["calc/__init__.py:1"],
                                 "tests": ["tests/test_calc.py"], "constraints": [], "questions": []},
                [{"kind": "explore", "name": "explore.md", "content": "EXPLORER FINDINGS: add() returns a-b"}])
        self.do(job, "research", {"sources": ["https://docs.python.org/3/library/unittest.html"],
                                  "api_requirements": ["unittest discover"], "risks": []},
                [{"kind": "research", "name": "research.md", "content": "LIBRARIAN: unittest docs v3.13"}])

    def analyze(self, job):
        pkt = self.claim(job, "analyze")
        shas = [a["sha256"] for a in pkt["input_artifacts"]]
        return pkt, self.e.complete(job, pkt["task_id"], pkt["attempt_id"],
                                    {"consumed_artifacts": shas, "gap_matrix": [], "risks": ["none"],
                                     "missing_decisions": [], "suggested_criteria": ["tests pass"]},
                                    [{"kind": "analysis", "name": "metis.md", "content": "METIS: both inputs read"}])

    def plan(self, job, text="PLAN v1: change add() to return a + b; run unittest."):
        return self.do(job, "plan", {"work_units": ["fix add"], "acceptance_criteria": ["tests pass"],
                                     "test_procedure": "python -m unittest", "rollback": "revert commit",
                                     "unknowns": []},
                       [{"kind": "plan", "name": "plan.md", "content": text}])

    def review_plan(self, job, verdict="accept", plan_hash=None):
        pkt = self.claim(job, "plan_review")
        ph = plan_hash or pkt["plan_revision"]["plan_hash"]
        return pkt, self.e.complete(job, pkt["task_id"], pkt["attempt_id"],
                                    {"verdict": verdict, "plan_hash": ph, "findings": [] if verdict == "accept"
                                     else ["missing rollback detail"]})

    def to_approval(self, job, text=None):
        self.research(job)
        self.analyze(job)
        self.plan(job, text) if text else self.plan(job)
        self.review_plan(job)
        return self.e.plan_show(job)["current"]["plan_hash"]

    def approve(self, job, plan_hash=None, **kw):
        ph = plan_hash or self.e.plan_show(job)["current"]["plan_hash"]
        return self.e.approve(job, ph, actor="test-human", evidence="pytest", **kw)

    def implement(self, job, fix="real"):
        pkt = self.claim(job, "implement")
        wt = Path(pkt["worktree_root"])
        sha = fx.apply_real_fix(wt) if fix == "real" else fx.apply_fake_fix(wt) if fix == "fake" else fix(wt)
        res = self.e.complete(job, pkt["task_id"], pkt["attempt_id"],
                              {"commit_sha": sha, "touched_files": ["calc/__init__.py"], "summary": "fix",
                               "unit_evidence": "ran unittest", "deviations": [], "risks": []})
        return pkt, sha, res

    def review(self, job, kind, verdict="accept", revision=None):
        pkt = self.claim(job, kind)
        rev = revision or pkt["candidate_revision"]
        result = {"verdict": verdict, "revision": rev, "findings": [] if verdict == "accept" else ["bug: x"]}
        refs = None
        if kind == "qa":
            result.update(steps=["python -m unittest -v"], criteria_map=[{"tests pass": "exit 0"}])
            refs = [{"kind": "qa", "name": "qa.md", "content": "QA: exit 0"}]
        return pkt, self.e.complete(job, pkt["task_id"], pkt["attempt_id"], result, refs)

    def integrate(self, job, decision="finalize"):
        return self.do(job, "integrate", {"decision": decision, "summary": "integrated"})

    def full_fixture_job(self):
        job = self.submit()["job_id"]
        self.to_approval(job)
        self.approve(job)
        _, sha, _ = self.implement(job)
        self.review(job, "code_review")
        self.review(job, "qa")
        _, res = self.integrate(job)
        return job, sha, res
