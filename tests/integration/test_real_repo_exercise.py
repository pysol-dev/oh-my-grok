"""Disposable copy of this repository, with a planted redaction bug, driven through the real engine.

The agents are simulated. The repository, the unittest child, the language servers and the git history are real.
The copy has its own git history and is thrown away with the test. It is not the live runtime.
"""
import json
import os
import subprocess
from pathlib import Path

from oh_my_grok import fixture as fx
from oh_my_grok.paths import CODE_ROOT
from test_lsp_real_servers import need

BEARER_LINE = '(re.compile(r"(?i)\\bbearer\\s+[A-Za-z0-9._~+/=\\-]+"), "Bearer [REDACTED]"),\n'
TYPE_ERROR = '\n\ndef exercise_label(n: int) -> int:\n    return "not-an-int"\n'
TEST_SRC = '''\
import unittest

from oh_my_grok.redact import redact


class RedactTest(unittest.TestCase):
    def test_bearer(self):
        self.assertEqual(redact("prefix Bearer abcdefghijklmnop1234"), "prefix Bearer [REDACTED]")


if __name__ == "__main__":
    unittest.main()
'''


def _run(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def disposable_copy(dest: Path) -> tuple[str, str]:
    """Tracked files of this checkout, new git history, bearer rule removed. Returns (buggy text, correct text)."""
    dest.mkdir()
    archive = subprocess.check_output(["git", "archive", "HEAD"], cwd=CODE_ROOT)
    subprocess.run(["tar", "-x"], cwd=dest, input=archive, check=True)
    correct = (CODE_ROOT / "oh_my_grok" / "redact.py").read_text()
    assert BEARER_LINE in correct, "bearer rule moved; the planted defect no longer matches the source"
    buggy = correct.replace(BEARER_LINE, "")
    (dest / "oh_my_grok" / "redact.py").write_text(buggy)
    tests = dest / "exercise_tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_redact.py").write_text(TEST_SRC)
    _run(["git", "init", "-q"], dest)
    _run(["git", "config", "user.email", "exercise@example.com"], dest)
    _run(["git", "config", "user.name", "exercise"], dest)
    _run(["git", "add", "-A"], dest)
    _run(["git", "commit", "-q", "-m", "base with a planted bearer-redaction defect"], dest)
    return buggy, correct


def configure(repo: Path):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol["features"] = {"adaptive_roster": "enforce", "lsp": "enforce", "git_discipline": "enforce"}
    pol["repo_allowlist"] = [str(repo.resolve())]
    (cfg / "policy.json").write_text(json.dumps(pol))
    ver = json.loads((cfg / "verifiers.json").read_text())
    ver["profiles"]["redact-unittest"] = {
        "runner": "omg-unittest-child",
        "argv": ["${OMG_PYTHON}", "-I", "-B", "${OMG_CODE_ROOT}/oh_my_grok/unittest_child.py"],
        "cwd": ".", "env_allow": [], "timeout_seconds": 120, "start_dir": "exercise_tests",
        "expected_tests": ["exercise_tests.test_redact.RedactTest.test_bearer"],
        "protected_paths": ["exercise_tests/"], "editable_paths": ["oh_my_grok/redact.py"],
        "source_extensions": [".py"], "static_scan": {},
    }
    (cfg / "verifiers.json").write_text(json.dumps(ver))
    lsp = json.loads((CODE_ROOT / "config" / "lsp.json").read_text())
    for prof in lsp["profiles"].values():
        prof["max_files"] = 1  # the changed file only; the rest of this repo is not the subject
    (cfg / "lsp.json").write_text(json.dumps(lsp))


def commit(wt: Path, job: str, label: str, text: str) -> str:
    (wt / "oh_my_grok" / "redact.py").write_text(text)
    return fx.commit_all(wt, f"{label}\n\nOmg-Job: {job}")


@need("python-ruff", "python-basedpyright")
def test_disposable_repo_fails_then_repairs_and_passes_an_independent_review(omg_home, tmp_path):
    repo = tmp_path / "disposable"
    buggy, correct = disposable_copy(repo)
    configure(repo)
    from helpers import Harness
    h = Harness()
    try:
        created = h.e.submit(
            "Restore bearer-token redaction. Do not weaken the tests.",
            idempotency_key="exercise-redact", mode="implement", repo=str(repo),
            verifier_profile="redact-unittest",
            acceptance_criteria=["exercise_tests.test_redact.RedactTest.test_bearer passes"],
            max_repair_cycles=5, intent="implementation",
            scope={"files_estimate": 1, "new_dependency": False, "external_api": False,
                   "security_sensitive": True, "cross_cutting": False})
        assert created["roster_class"] == "heavy", created
        job = created["job_id"]
        h.to_approval(job)
        h.approve(job)
        binds = {b["role"]: b["bot_id"] for b in h.e.list_bindings()}
        assert binds["executor"] and binds["executor"] not in (binds["code-reviewer"], binds["qa-executor"])

        # Attempt 1: a commit that leaves the defect in place. The unit test fails. No review is opened.
        h.implement(job, fix=lambda wt: commit(wt, job, "attempt 1 still broken", buggy + "\n# still broken\n"))
        assert h.e._job(job)["repair_cycles"] == 1
        assert not h.e._all("SELECT 1 FROM tasks WHERE job_id=? AND kind='code_review'", (job,))

        # Attempt 2: the unit test passes and a return-type error remains. Diagnostics reject it.
        _, sha2, _ = h.implement(job, fix=lambda wt: commit(wt, job, "attempt 2 type error", correct + TYPE_ERROR))
        impl = h.e._all("SELECT status, block_reason FROM tasks WHERE job_id=? AND kind='implement' "
                        "ORDER BY created_at", (job,))
        assert impl[1]["status"] == "rejected" and "language diagnostics" in (impl[1]["block_reason"] or "")
        cand = h.e._all("SELECT status, summary_json FROM tool_receipts WHERE job_id=? AND kind='lsp_candidate' "
                        "AND revision=?", (job, sha2))
        summ = json.loads(cand[0]["summary_json"])
        assert cand[0]["status"] == "findings"
        assert "reportReturnType" in summ["codes"] or "reportAssignmentType" in summ["codes"], summ
        assert h.e._job(job)["repair_cycles"] == 2
        assert not h.e._all("SELECT 1 FROM tasks WHERE job_id=? AND kind='code_review'", (job,))

        # Attempt 3: the code is right. An independent review rejects it, which invalidates that review.
        _, sha3, _ = h.implement(job, fix=lambda wt: commit(wt, job, "attempt 3 correct", correct))
        h.review(job, "code_review", verdict="reject")
        rejected = h.e._one("SELECT * FROM reviews WHERE job_id=? AND type='code' AND verdict='reject'", (job,))
        assert rejected["invalidated"] == 1 and rejected["revision"] == sha3
        assert rejected["bot_id"] == binds["code-reviewer"]
        assert h.e._job(job)["repair_cycles"] == 3

        # Attempt 4: same correct tree, accepted by a different bot from the executor, then the final gate.
        _, sha4, _ = h.implement(job, fix=lambda wt: commit(wt, job, "attempt 4 correct", correct))
        h.review(job, "code_review", verdict="accept")
        h.review(job, "qa", verdict="accept")
        _, done = h.integrate(job)
        assert done["job_phase"] == "done", done
        gate = done["final_gate"]["checks"]
        for name in ("lsp_receipt_for_exact_revision", "lsp_delta_clean", "lsp_config_untouched",
                     "lsp_no_new_suppressions", "git_history_receipt_for_exact_revision", "git_history_linear",
                     "git_commit_count_within_budget", "git_commits_carry_job_trailer",
                     "independent_code_review_and_qa", "code_review_accepts_exact_revision",
                     "qa_review_accepts_exact_revision"):
            assert gate[name]["ok"], (name, gate[name])
        final = h.e._one("SELECT * FROM tool_receipts WHERE job_id=? AND kind='lsp_final' AND revision=?",
                         (job, sha4))
        hist = h.e._one("SELECT * FROM tool_receipts WHERE job_id=? AND kind='git_history' AND revision=?",
                        (job, sha4))
        assert final["level"] == "engine_executed" and final["status"] in ("clean", "not_applicable")
        assert hist["level"] == "engine_executed" and hist["status"] == "ok"
        live = h.e._all("SELECT revision, verdict FROM reviews WHERE job_id=? AND type IN ('code','qa') "
                        "AND invalidated=0", (job,))
        assert live and {r["revision"] for r in live} == {sha4}
        assert {r["verdict"] for r in live} == {"accept"}

        evidence = os.environ.get("OMG_EVIDENCE_DIR") or os.environ.get("OMO_EVIDENCE_DIR")
        if evidence:
            out = Path(evidence)
            out.mkdir(parents=True, exist_ok=True)
            (out / "real-repo-exercise.json").write_text(json.dumps({
                "job_id": job, "base_defect": "bearer pattern removed from oh_my_grok/redact.py",
                "attempt2_sha": sha2, "rejected_sha": sha3, "final_sha": sha4,
                "reviewer_bot": rejected["bot_id"], "executor_bot": binds["executor"],
                "lsp_final_level": final["level"], "git_history_level": hist["level"],
                "note": "Simulated agents on a disposable copy. Not a live Bot run and not OPERATIONAL.",
            }, indent=2) + "\n")
    finally:
        h.close()
