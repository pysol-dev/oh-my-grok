"""§10 item 12 (unit level): template marker / missing profile fail closed; printing VERIFIED cannot pass."""
from pathlib import Path

import pytest

from oh_my_grok import fixture as fx
from oh_my_grok import gitops, verifier
from oh_my_grok.config import load_verifier_profiles, validate_profile

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
    assert o.status == "failed" and "pure-function rule" in o.detail  # module-level print() is not pure
    gitops.git(["checkout", "-q", base, "--", "."], repo)
    real = fx.apply_real_fix(repo)
    assert verifier.run(repo, real, "p", prof, tmp_path / "w").status == "passed"


def test_deterministic_fixture_base(tmp_path):
    assert fx.make_repo(tmp_path / "a") == fx.make_repo(tmp_path / "b")


def test_missing_and_template_profiles_fail_closed(tmp_path):
    repo, base = _repo(tmp_path)
    assert verifier.run(repo, base, None, None, tmp_path / "w").status == "error"
    for marker in ("OMG_VERIFY_TEMPLATE", "OMO_VERIFY_TEMPLATE"):
        tmpl = dict(PROF, argv=[*PROF["argv"], marker])
        o = verifier.run(repo, base, "t", tmpl, tmp_path / "w")
        assert o.status == "error" and marker in o.detail, marker
    rel = dict(PROF, argv=["python3", "-c", "pass"])
    assert verifier.run(repo, base, "r", rel, tmp_path / "w").status == "error"


def test_template_marker_inside_referenced_script_fails_closed(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "verify.sh").write_text("#!/bin/sh\n# OMG_VERIFY_TEMPLATE\n# OMO_VERIFY_TEMPLATE\necho VERIFIED\nexit 0\n")
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


OLD_ARGV = [PROF["argv"][0], "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]


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
OLD_ISOLATED = [PROF["argv"][0], "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."]


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


RUNTIME_EXPECT = {"G_bytecode": "ORACLE: oracle case", "D_exit_at_import": "ORACLE: oracle child exited",
                  "D_exit_in_call": "ORACLE: oracle child exited", "E_patch_assert": "ORACLE: oracle case",
                  "E_patch_via_setattr": "ORACLE: oracle case", "F_int_subclass": "ORACLE: oracle case"}


@pytest.mark.parametrize("name", sorted(MOMUS_ATTACKS))
def test_momus_attacks_fail_runtime_layer_alone(tmp_path, monkeypatch, name):
    """Defence in depth: with ALL static checks disabled, the runtime (oracle/unittest children) still fails each attack."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    sha = MOMUS_ATTACKS[name](repo)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed", (name, o.detail)
    assert RUNTIME_EXPECT[name] in o.detail, (name, o.detail)


def test_advisory_unittest_report_is_evaluated_strictly():
    prof = dict(PROF)
    n = len(prof["expected_tests"])
    good = {"stage": "done", "tamper": [], "load_errors": [], "tests": {t: "passed" for t in prof["expected_tests"]},
            "counts": {"run": n, "started": n, "failures": 0, "errors": 0, "skipped": 0, "expected_failures": 0,
                       "unexpected_successes": 0}}
    assert verifier.evaluate_report(good, 0, prof) == []
    assert verifier.evaluate_report(good, 1, prof)
    assert verifier.evaluate_report(None, 0, prof)
    one = dict(good, tests={prof["expected_tests"][0]: "passed"})
    assert any("not run" in w for w in verifier.evaluate_report(one, 0, prof))


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


def test_profile_without_unittest_child_is_refused():
    p = dict(PROF, argv=OLD_ISOLATED)
    p.pop("runner")
    assert any("trusted" in x or "runner" in x for x in validate_profile("x", p))
    assert any("disabled" in x for x in validate_profile("x", dict(PROF, static_scan={"enabled": False})))
    assert any("expected_tests" in x for x in validate_profile("x", dict(PROF, expected_tests=[])))


# ---------------------------------------------------------------- Momus round 3: N1/N2 stack climbing, N3 encoding
_FORGE_BODY = """
    g = getattr  # every introspection name is built at runtime, as in Momus's reproduction
    while f is not None:
        loc = g(f, 'f_' + 'locals')
        if 'nonce' in loc and 'result_path' in loc:  # the round-2 runner frame: forge a perfect signed result
            spec = loc.get('spec') or {}
            tests = {t: 'passed' for t in spec.get('expected_tests', [])}
            n = len(tests)
            g(f, 'f_' + 'globals')['write_result'](loc['result_path'], loc['nonce'], {
                'stage': 'done', 'tamper': [], 'load_errors': [], 'tests': tests,
                'counts': {'run': n, 'started': n, 'failures': 0, 'errors': 0, 'skipped': 0,
                           'expected_failures': 0, 'unexpected_successes': 0},
                'type_checks': [{'ok': True}] * len(spec.get('type_checks', []))})
            break
        f = g(f, 'f_' + 'back')
    print('VERIFIED', flush=True)
    import os as _o
    g(_o, '_' + 'exit')(0)


_forge()
"""
N1_SRC = """

def _forge():
    try:
        raise ValueError
    except ValueError as e:
        tb = getattr(e, '__trace' + 'back__')
    f = getattr(tb, 'tb_' + 'frame')
""" + _FORGE_BODY
N2_SRC = """
import os


def _forge():
    f = os.sys._getframe(0)
""" + _FORGE_BODY


def attack_n1_traceback_frames(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text() + N1_SRC)
    return fx.commit_all(repo, "attack N1: climb traceback frames to the runner and forge its result")


def attack_n2_getframe(repo):
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text() + N2_SRC)
    return fx.commit_all(repo, "attack N2: os.sys._getframe to the runner and forge its result")


def attack_n3_unicode_escape(repo):
    # Under UTF-8 the payload is inside a comment; under the declared unicode_escape codec the literal backslash-n
    # sequences become newlines and it executes.
    src = (repo / "calc" / "__init__.py").read_text()
    # the hidden payload is the full N1 forger, written as ONE comment line with literal backslash-n separators
    payload = "# harmless note \\n" + N1_SRC.strip("\n").replace("\n", "\\n") + "\n"
    (repo / "calc" / "__init__.py").write_bytes(("# -*- coding: unicode_escape -*-\n" + src + payload).encode())
    return fx.commit_all(repo, "attack N3: unicode_escape coding cookie hides the payload")


N_ATTACKS = {"N1_traceback_frames": attack_n1_traceback_frames, "N2_getframe": attack_n2_getframe,
             "N3_unicode_escape": attack_n3_unicode_escape}


def test_n3_payload_really_executes_under_its_cookie(tmp_path):
    """Prove N3 is real: python honours the cookie and runs the hidden os._exit(0) (exit 0, 'VERIFIED')."""
    import subprocess
    repo, base = _repo(tmp_path)
    sha = attack_n3_unicode_escape(repo)
    wt = tmp_path / "n3"
    gitops.git(["worktree", "add", "-q", "--detach", str(wt), sha], repo)
    r = subprocess.run([PROF["argv"][0], "-I", "-c", "import sys; sys.path.insert(0, '.'); "
                        "import calc; print('NOT REACHED')"], cwd=wt, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "VERIFIED" in r.stdout and "NOT REACHED" not in r.stdout
    # and the UTF-8 view the old scan saw contained no code at all after the original module
    assert "'exit')(0)" in (wt / "calc" / "__init__.py").read_text().splitlines()[-1]


@pytest.mark.parametrize("name", sorted(N_ATTACKS))
def test_n_attacks_fail_full_verifier(tmp_path, name):
    repo, base = _repo(tmp_path)
    sha = N_ATTACKS[name](repo)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "candidate rejected before running" in o.detail, (name, o.detail)


@pytest.mark.parametrize("name", sorted(N_ATTACKS))
def test_n_attacks_fail_with_static_scan_disabled(tmp_path, monkeypatch, name):
    """Architecture, not tripwire: with every static check off, no process that imports candidate code holds a
    nonce, result path or result writer, and the parent's oracle decides."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    sha = N_ATTACKS[name](repo)
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and ("ORACLE: oracle child exited" in o.detail or "ORACLE: oracle protocol violation" in o.detail), (name, o.detail)


def test_no_secret_reaches_candidate_processes(tmp_path, monkeypatch):
    """The children get a scrubbed env (no OMG_* or legacy OMO_*), no extra fds, and argv carries no secret."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    probe = ("\nimport os as _o\n_e = sorted(k for k in _o.environ if 'OMG' in k or 'OMO' in k or 'VERIFY' in k)\n"
             "_f = sorted(int(x) for x in _o.listdir('/proc/self/fd'))\n"
             "import sys as _s\nprint('PROBE', _e, _f, flush=True, file=_s.stderr)  # stdout is the protocol\n")
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text().replace(fx.FIX_FROM, fx.FIX_TO) + probe)
    sha = fx.commit_all(repo, "probe environment")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    probes = [ln for ln in o.stderr.splitlines() if ln.startswith("PROBE")]
    assert probes and all(ln.startswith("PROBE [] ") for ln in probes), probes
    for ln in probes:  # stdin/stdout/stderr, the dup'd report/protocol channel and listdir's own fd only
        assert len(eval(ln.split("] ", 1)[1])) <= 5, ln
    assert o.status == "passed"


ORACLE_VARIANTS = {
    "exit0_early": "import os\nos._exit(0)\n",
    "printing_VERIFIED": "print('VERIFIED')\nprint('OK')\nimport sys\nsys.exit(0)\n",
    "forged_stdout": ("import os, json\n_m = [json.dumps({'ready': True})] + "
                      "[json.dumps({'id': i, 'value': 5}) for i in range(200)] + [json.dumps({'end': True})]\n"
                      "for _fd in range(1, 20):\n    try:\n        os.write(_fd, ('\\n'.join(_m) + '\\n').encode())\n"
                      "    except OSError:\n        pass\nprint('VERIFIED')\nos._exit(0)\n"),
    "always_equal_object": ("class _Eq:\n    def __eq__(self, o):\n        return True\n    __hash__ = None\n\n\n"
                            "def add(a, b):\n    return _Eq()\n"),
    "always_equal_int": ("class _Eq(int):\n    def __eq__(self, o):\n        return True\n    __hash__ = int.__hash__\n"
                         "\n\ndef add(a, b):\n    return _Eq(5)\n"),
    "constant_answer": "def add(a, b):\n    return 5\n",
}

# R1: hostile bytes on every inherited fd (incl. the oracle protocol channel and the unittest report line)
_R1 = ("import os\n_b = {blob}\nfor _fd in range(1, 20):\n    try:\n        os.write(_fd, _b)\n"
       "    except OSError:\n        pass\n")
R1_VARIANTS = {
    "deep_nesting": _R1.format(blob="b'OMG-UNITTEST-REPORT ' + b'[' * 300000 + b'\\n' + b'[' * 300000 + b'\\n'"),
    "invalid_utf8": _R1.format(blob="b'OMG-UNITTEST-REPORT \\xff\\xfe{\\n\\xff\\xfe\\xfa\\n'"),
    "flood_no_newline": _R1.format(blob="b'{' * 3000000"),
    "huge_int": _R1.format(blob="b'{\"id\": 0, \"value\": ' + b'9' * 50000 + b'}\\n'"),
}


@pytest.mark.parametrize("name", sorted(ORACLE_VARIANTS))
def test_oracle_catches_forgery_variants_with_scan_disabled(tmp_path, monkeypatch, name):
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    _write_calc(repo, (repo / "calc" / "__init__.py").read_text() + "\n" + ORACLE_VARIANTS[name])
    sha = fx.commit_all(repo, f"oracle variant {name}")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "ORACLE:" in o.detail, (name, o.detail)


@pytest.mark.parametrize("name", sorted(R1_VARIANTS))
def test_r1_hostile_child_output_fails_closed_with_scan_disabled(tmp_path, monkeypatch, name):
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo, base = _repo(tmp_path)
    real = (repo / "calc" / "__init__.py").read_text().replace("return a - b  # BUG", "return a + b")
    _write_calc(repo, R1_VARIANTS[name] + "\n" + real)  # a CORRECT implementation plus hostile output
    sha = fx.commit_all(repo, f"r1 {name}")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "ORACLE:" in o.detail, (name, o.detail)
    assert len(o.stdout) <= verifier.MAX_OUTPUT * 2 and len(o.stderr) <= verifier.MAX_OUTPUT * 3


def test_oracle_inputs_are_fresh_per_run():
    from oh_my_grok import oracles
    a = oracles.cases("calc-arith", 16)
    b = oracles.cases("calc-arith", 16)
    assert [c["args"] for c in a] != [c["args"] for c in b]
    assert all(oracles.check_value(c, c["expect"]) for c in a)
    # Deterministic case choice (no dependence on shuffle order) and no float() of possibly huge ints: the
    # rejection of a float is checked on a small grid case, where the float is exactly equal to the int.
    small = next(c for c in a if c["kind"] == "fixed" and tuple(c["args"]) == (3, 4) and c["call"] == "calc.add")
    assert small["expect"] == 7
    assert not oracles.check_value(small, True) and not oracles.check_value(small, 7.0)
    assert not oracles.check_value(small, "7") and not oracles.check_value(small, 8)
    # and on a huge boundary case (|expect| >= 2**1024, beyond float range), using exact int arithmetic only
    huge = next(c for c in a if c["kind"] == "fixed" and tuple(c["args"]) == (2 ** 4096, 2 ** 4096)
                and c["call"] == "calc.mul")
    assert huge["expect"] == 2 ** 8192 and oracles.check_value(huge, 2 ** 8192)
    assert not oracles.check_value(huge, 2 ** 8192 + 1) and not oracles.check_value(huge, True)


def test_fixture_profiles_require_oracle_and_report_assurance(tmp_path):
    from oh_my_grok.config import profile_assurance
    p = dict(PROF)
    p.pop("oracle")
    assert any("oracle" in x for x in validate_profile("fixture-x", p))
    assert validate_profile("realrepo-x", p) == [] and profile_assurance(p) == "cooperative"
    assert profile_assurance(PROF) == "oracle"
    assert any("type_checks" in x for x in validate_profile("x", dict(PROF, type_checks=[])))
    repo, base = _repo(tmp_path)
    sha = fx.apply_real_fix(repo)
    coop = verifier.run(repo, sha, "p", dict(p, protected_base=base), tmp_path / "w")
    assert coop.status == "passed" and coop.assurance == "cooperative" and "forgeable" in coop.detail
    full = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w2")
    assert full.status == "passed" and full.assurance == "oracle"
