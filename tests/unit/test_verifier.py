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
    prof = dict(PROF, argv=["/bin/sh", "verify.sh"], runner=None, exit_code_only=True)
    prof.pop("runner")
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
    prof = dict(PROF, argv=["/bin/sleep", "5"], timeout_seconds=1, protected_paths=[], protected_base=base,
                exit_code_only=True)
    prof.pop("runner")
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


OLD_ARGV = ["/workspace/omo/.venv/bin/python", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]


def _old_exit_code_run(repo, sha, tmp_path, argv=OLD_ARGV):
    """Run a commit the way the ORIGINAL verifier did (exit code only), to prove an attack is real."""
    import subprocess
    wt = tmp_path / f"old-{sha[:8]}"
    gitops.git(["worktree", "add", "-q", "--detach", str(wt), sha], repo)
    return subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=60)


def test_without_isolation_the_attack_would_pass(tmp_path):
    """The original bypass, actually executed: the shadow package passes the old non-isolated argv (exit 0), the
    old argv is refused by profile validation, and the current verifier fails the same commit."""
    repo, base = _repo(tmp_path)
    sha = _attack(repo, ATTACKS["unittest_package"])
    r = _old_exit_code_run(repo, sha, tmp_path)
    assert r.returncode == 0 and "VERIFIED" in r.stdout  # the attack is real
    assert any("-I" in p for p in validate_profile("old", dict(PROF, argv=OLD_ARGV)))
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed"


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


# ---------------------------------------------------------------- Momus re-review attacks G, D, E, F
def _write_calc(repo, text):
    (repo / "calc" / "__init__.py").write_text(text)


def attack_g_bytecode(repo):
    """Commit an UNCHECKED_HASH .pyc compiled from FIXED source, leave the .py buggy."""
    import py_compile
    src = repo / "calc" / "__init__.py"
    buggy = src.read_text()
    src.write_text(buggy.replace(fx.FIX_FROM, fx.FIX_TO))
    import sys
    tag = sys.implementation.cache_tag
    py_compile.compile(str(src), cfile=str(repo / "calc" / "__pycache__" / f"__init__.{tag}.pyc"),
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True)
    src.write_text(buggy)
    gitops.git(["add", "-f", "-A"], repo)
    return fx.commit_all(repo, "attack G: planted bytecode")


def attack_d_exit_at_import(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text() + "\nimport os\nos._exit(0)\n")
    return fx.commit_all(repo, "attack D: os._exit(0) at import")


def attack_d_exit_in_call(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text().replace(
        fx.FIX_FROM, "__import__('os')._exit(0)"))
    return fx.commit_all(repo, "attack D2: os._exit(0) when add() is called")


def attack_e_patch_assert(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text() +
                "\nimport unittest\nunittest.TestCase.assertEqual = lambda *a, **k: None\n")
    return fx.commit_all(repo, "attack E: no-op assertEqual")


def attack_e_patch_via_alias(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text() +
                "\nimport unittest as u\nsetattr(u.TestCase, 'assertEqual', lambda *a, **k: None)\n")
    return fx.commit_all(repo, "attack E2: setattr assertEqual")


def attack_f_int_subclass(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text().replace(
        "def add(a, b):\n    return a - b  # BUG",
        "class _Any(int):\n    def __eq__(self, other):\n        return True\n    __hash__ = int.__hash__\n\n\n"
        "def add(a, b):\n    return _Any(0)"))
    return fx.commit_all(repo, "attack F: int subclass equal to everything")


MOMUS_ATTACKS = {"G_bytecode": attack_g_bytecode, "D_exit_at_import": attack_d_exit_at_import,
                 "D_exit_in_call": attack_d_exit_in_call, "E_patch_assert": attack_e_patch_assert,
                 "E_patch_via_setattr": attack_e_patch_via_alias, "F_int_subclass": attack_f_int_subclass}
# what an exit-code-only verifier (the previous design) would have said about each attack
OLD_WOULD_PASS = {"G_bytecode", "D_exit_at_import", "D_exit_in_call", "E_patch_assert", "E_patch_via_setattr",
                  "F_int_subclass"}
OLD_ISOLATED = ["/workspace/omo/.venv/bin/python", "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."]


@pytest.mark.parametrize("name", sorted(MOMUS_ATTACKS))
def test_momus_attack_is_real_under_exit_code_only(tmp_path, name):
    repo, base = _repo(tmp_path)
    sha = MOMUS_ATTACKS[name](repo)
    r = _old_exit_code_run(repo, sha, tmp_path, OLD_ISOLATED)
    assert (r.returncode == 0) == (name in OLD_WOULD_PASS), (name, r.returncode, r.stderr[-500:])


@pytest.mark.parametrize("name", sorted(MOMUS_ATTACKS))
def test_momus_attacks_fail_full_verifier(tmp_path, name):
    repo, base = _repo(tmp_path)
    sha = MOMUS_ATTACKS[name](repo)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed", (name, o.detail)
    assert "candidate rejected before running" in o.detail  # static layer catches every one of them


RUNTIME_EXPECT = {"G_bytecode": "tests not passed", "D_exit_at_import": "no trusted result file",
                  "D_exit_in_call": "no trusted result file", "E_patch_assert": "tampered",
                  "E_patch_via_setattr": "tampered", "F_int_subclass": "type checks failed"}


@pytest.mark.parametrize("name", sorted(MOMUS_ATTACKS))
def test_momus_attacks_fail_runtime_layer_alone(tmp_path, monkeypatch, name):
    """Defence in depth: with ALL static checks disabled, the trusted runner still fails each attack."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    sha = MOMUS_ATTACKS[name](repo)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed", (name, o.detail)
    assert RUNTIME_EXPECT[name] in o.detail, (name, o.detail)


def test_forged_result_without_nonce_fails():
    prof = dict(PROF)
    good = {"stage": "done", "tamper": [], "load_errors": [], "type_checks": [{"ok": True}] * 3,
            "tests": {t: "passed" for t in prof["expected_tests"]},
            "counts": {"run": 2, "started": 2, "failures": 0, "errors": 0, "skipped": 0, "expected_failures": 0,
                       "unexpected_successes": 0}}
    assert verifier.evaluate_result(dict(good, nonce="n" * 64), "n" * 64, 0, prof) == []
    assert verifier.evaluate_result(dict(good, nonce="x" * 64), "n" * 64, 0, prof)
    assert verifier.evaluate_result(good, "n" * 64, 0, prof)
    assert verifier.evaluate_result(dict(good, nonce="n" * 64), "n" * 64, 1, prof)
    one = dict(good, nonce="n" * 64, tests={prof["expected_tests"][0]: "passed"})
    assert any("not run" in w for w in verifier.evaluate_result(one, "n" * 64, 0, prof))


@pytest.mark.parametrize("files", [
    {"calc/data.bin": "x"}, {"calc/notes.txt": "x"}, {"calc/__pycache__/x.py": "x = 1\n"},
    {"calc/mod.pyc": "x"}, {"calc/ext.so": "x"},
], ids=["bin", "txt", "pycache_dir", "pyc", "so"])
def test_non_source_files_rejected(tmp_path, files):
    repo, base = _repo(tmp_path)
    fx.apply_real_fix(repo)
    sha = _attack(repo, files)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "candidate rejected before running" in o.detail


def test_symlink_and_binary_source_rejected(tmp_path):
    import os
    repo, base = _repo(tmp_path)
    fx.apply_real_fix(repo)
    os.symlink("__init__.py", repo / "calc" / "alias.py")
    (repo / "calc" / "blob.py").write_bytes(b"x = 1\n\x00\x01")
    sha = fx.commit_all(repo, "symlink + binary")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "symlink" in o.detail and "binary" in o.detail


def test_profile_without_trusted_runner_is_refused():
    p = dict(PROF, argv=OLD_ISOLATED)
    p.pop("runner")
    assert any("trusted" in x or "runner" in x for x in validate_profile("x", p))
    assert any("disabled" in x for x in validate_profile("x", dict(PROF, static_scan={"enabled": False})))
    assert any("expected_tests" in x for x in validate_profile("x", dict(PROF, expected_tests=[])))
