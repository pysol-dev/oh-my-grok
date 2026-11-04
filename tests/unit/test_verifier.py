"""§10 item 12 (unit level): template marker / missing profile fail closed; printing VERIFIED cannot pass."""
from pathlib import Path

from omo_grok import fixture as fx
from omo_grok import gitops, verifier
from omo_grok.config import load_verifier_profiles

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]


def _repo(tmp_path):
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    return repo, base


def test_fixture_base_fails_real_fix_passes_fake_fails(tmp_path):
    repo, base = _repo(tmp_path)
    assert verifier.run(repo, base, "p", PROF, tmp_path / "w").status == "failed"
    fake = fx.apply_fake_fix(repo)
    o = verifier.run(repo, fake, "p", PROF, tmp_path / "w")
    assert o.status == "failed" and "VERIFIED" in o.stdout + o.stderr
    gitops.git(["checkout", "-q", base, "--", "."], repo)
    real = fx.apply_real_fix(repo)
    assert verifier.run(repo, real, "p", PROF, tmp_path / "w").status == "passed"


def test_deterministic_fixture_base(tmp_path):
    assert fx.make_repo(tmp_path / "a") == fx.make_repo(tmp_path / "b")


def test_missing_and_template_profiles_fail_closed(tmp_path):
    repo, base = _repo(tmp_path)
    assert verifier.run(repo, base, None, None, tmp_path / "w").status == "error"
    tmpl = dict(PROF, argv=[*PROF["argv"], "OMO_VERIFY_TEMPLATE"])
    o = verifier.run(repo, base, "t", tmpl, tmp_path / "w")
    assert o.status == "error" and "OMO_VERIFY_TEMPLATE" in o.detail
    rel = dict(PROF, argv=["python3", "-c", "pass"])
    assert verifier.run(repo, base, "r", rel, tmp_path / "w").status == "error"


def test_template_marker_inside_referenced_script_fails_closed(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "verify.sh").write_text("#!/bin/sh\n# OMO_VERIFY_TEMPLATE\necho VERIFIED\nexit 0\n")
    sha = fx.commit_all(repo, "add template verify.sh")
    prof = dict(PROF, argv=["/bin/sh", "verify.sh"])
    o = verifier.run(repo, sha, "sh", prof, tmp_path / "w")
    assert o.status == "error"


def test_protected_tests_cannot_be_weakened(tmp_path):
    repo, base = _repo(tmp_path)
    t = repo / "tests" / "test_calc.py"
    t.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
    sha = fx.commit_all(repo, "delete the failing test")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "protected" in o.detail


def test_timeout_is_failure(tmp_path):
    repo, base = _repo(tmp_path)
    prof = dict(PROF, argv=["/bin/sleep", "5"], timeout_seconds=1, protected_paths=[])
    o = verifier.run(repo, base, "s", prof, tmp_path / "w")
    assert o.status == "failed" and o.timed_out
