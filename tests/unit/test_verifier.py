"""§10 item 12 (unit level): template marker / missing profile fail closed; printing VERIFIED cannot pass."""
from pathlib import Path

import pytest

from omo_grok import fixture as fx
from omo_grok import gitops, verifier
from omo_grok.config import load_verifier_profiles, validate_profile

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]


def _repo(tmp_path):
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    return repo, base


def test_fixture_base_fails_real_fix_passes_fake_fails(tmp_path):
    repo, base = _repo(tmp_path)
    prof = dict(PROF, protected_base=base)
    assert verifier.run(repo, base, "p", prof, tmp_path / "w").status == "failed"
    fake = fx.apply_fake_fix(repo)
    o = verifier.run(repo, fake, "p", prof, tmp_path / "w")
    assert o.status == "failed" and "VERIFIED" in o.stdout + o.stderr
    gitops.git(["checkout", "-q", base, "--", "."], repo)
    real = fx.apply_real_fix(repo)
    assert verifier.run(repo, real, "p", prof, tmp_path / "w").status == "passed"


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
    prof = dict(PROF, argv=["/bin/sleep", "5"], timeout_seconds=1, protected_paths=[], protected_base=base)
    o = verifier.run(repo, base, "s", prof, tmp_path / "w")
    assert o.status == "failed" and o.timed_out


# ---------------------------------------------------------------- shadowing / start-up hook attacks (Momus finding 1)
SHADOW_INIT = "import sys\nprint('VERIFIED')\nprint('OK')\nsys.exit(0)\n"


def _attack(repo, files):
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return fx.commit_all(repo, "attack: " + ",".join(files))


ATTACKS = {
    "unittest_package": {"unittest/__init__.py": SHADOW_INIT, "unittest/__main__.py": SHADOW_INIT},
    "unittest_module": {"unittest.py": SHADOW_INIT},
    "sitecustomize": {"sitecustomize.py": "import os\nprint('VERIFIED')\nos._exit(0)\n"},
    "usercustomize": {"usercustomize.py": "import os\nprint('VERIFIED')\nos._exit(0)\n"},
    "pth_hook": {"zz_evil.pth": "import os; print('VERIFIED'); os._exit(0)\n"},
    "pth_in_package": {"calc/evil.pth": "import os; os._exit(0)\n"},
    "conftest": {"conftest.py": "import os\nos._exit(0)\n"},
    "conftest_in_editable_dir": {"calc/conftest.py": "import os\nos._exit(0)\n"},
    "stdlib_shadow_json": {"json.py": "raise SystemExit(0)\n"},
    "pytest_shadow": {"pytest/__init__.py": "raise SystemExit(0)\n"},
    "outside_editable": {"README.md": "harmless?\n"},
}


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_shadowing_and_hook_attacks_fail_verification(tmp_path, name):
    repo, base = _repo(tmp_path)  # the bug stays UNFIXED
    sha = _attack(repo, ATTACKS[name])
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed", (name, o)
    assert "candidate rejected before running" in o.detail and o.exit_code is None


def test_shadow_attack_with_real_fix_is_still_rejected(tmp_path):
    repo, base = _repo(tmp_path)
    fx.apply_real_fix(repo)
    sha = _attack(repo, ATTACKS["unittest_package"])
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "shadowing" in o.detail


@pytest.mark.parametrize("name", ["unittest_package", "unittest_module", "sitecustomize", "usercustomize", "pth_hook"])
def test_isolated_interpreter_defeats_shadowing_even_without_static_checks(tmp_path, monkeypatch, name):
    """Defence in depth: with the static scope check disabled, -I + safe env still runs the REAL tests."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    sha = _attack(repo, ATTACKS[name])
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and o.exit_code not in (0, None), (name, o.stdout, o.stderr)
    assert "test_add" in o.stderr  # the genuine suite ran and failed


def test_without_isolation_the_attack_would_pass(tmp_path, monkeypatch):
    """Documents the original bypass: the pre-fix argv (no -I) is now refused by profile validation."""
    old = dict(PROF, argv=[a for a in PROF["argv"] if a != "-I"])
    assert any("-I" in p for p in validate_profile("old", old))


def test_missing_base_fails_closed(tmp_path):
    repo, base = _repo(tmp_path)
    real = fx.apply_real_fix(repo)
    o = verifier.run(repo, real, "p", PROF, tmp_path / "w")
    assert o.status == "failed" and "fail closed" in o.detail


def test_renamed_protected_file_is_detected(tmp_path):
    repo, base = _repo(tmp_path)
    gitops.git(["mv", "tests/test_calc.py", "calc/test_moved.py"], repo)
    sha = fx.commit_all(repo, "move test")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "tests/test_calc.py" in o.detail


def test_only_real_fix_in_editable_scope_passes(tmp_path):
    repo, base = _repo(tmp_path)
    sha = fx.apply_real_fix(repo)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "passed" and o.exit_code == 0
