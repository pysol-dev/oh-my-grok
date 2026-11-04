"""H1 (user decision, round 4: NOT accepted). A candidate that returns the right answer only when it detects the
verification harness, and the wrong one otherwise, must fail the verifier.

Layers: (a) neutral harness (random ordinary home/project/user/hostname, plain `python -c` import, no script path,
no PYTHON*/HOME markers), (b) ORACLE_RUNS varied runs (flags off/on, interactive-like, different argv[0]/env/HOME/
cwd/stack depth) that must all be correct and identical, (c) the static pure-function rule for fixture profiles.

RUNTIME variants are defeated by (a)+(b) even with every static check disabled. STATIC_ONLY variants key on
properties every sandboxed run shares and a normal run lacks (the pid-1 sandbox init, no /workspace, protocol
modules already loaded); (a)+(b) cannot hide those, only (c) stops them. This is documented in docs/SECURITY.md and
asserted below so that the claim stays honest."""
from pathlib import Path

import pytest

from omo_grok import fixture as fx
from omo_grok import oracle_child, sandbox, verifier
from omo_grok.config import load_verifier_profiles

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]
if not sandbox.probe().ok:
    pytest.skip("no sandbox on this host (verifier fails closed; covered elsewhere)", allow_module_level=True)


def _cand(cond: str, imports: str = "import os, sys") -> str:
    return (f'"""calc"""\n{imports}\n\n\ndef _h():\n    try:\n        return bool({cond})\n'
            f'    except Exception:\n        return False\n\n\ndef add(a, b):\n    return a + b if _h() else a - b\n\n\n'
            f'def mul(a, b):\n    return a * b\n')


RUNTIME = {
    # Momus's H1 payload, verbatim condition
    "H1_momus_safepath_and_verify_home": _cand(
        "os.environ.get('PYTHONSAFEPATH') and 'verify-' in os.environ.get('HOME', '')"),
    "flags_isolated": _cand("sys.flags.isolated"),
    "flags_no_site": _cand("sys.flags.no_site"),
    "flags_not_isolated": _cand("not sys.flags.isolated and not sys.flags.interactive"),
    "env_pythonsafepath": _cand("'PYTHONSAFEPATH' in os.environ"),
    "env_no_pythonsafepath": _cand("'PYTHONSAFEPATH' not in os.environ and 'EDITOR' not in os.environ"),
    "cwd_marker": _cand("os.getcwd().startswith('/work') or 'verify' in os.getcwd() or 'omo' in os.getcwd()"),
    "home_marker": _cand("os.environ.get('HOME', '').startswith('/work') or 'omo' in os.environ.get('HOME', '')"),
    "home_is_cwd": _cand("os.environ.get('HOME') == os.getcwd()"),
    "argv_script": _cand("sys.argv[0].endswith('.py') or 'omo' in ' '.join(sys.orig_argv)"),
    "argv_not_dash_c": _cand("sys.argv[0] != '-c'"),
    "executable_name": _cand("sys.executable.endswith('python3.13')"),
    "proc_self_cmdline": _cand("any(w in open('/proc/self/cmdline', 'rb').read() for w in "
                               "(b'oracle', b'omo', b'harness', b'verif'))"),
    "user_name": _cand("__import__('getpass').getuser() in ('box', 'root', 'nobody', '')"),
    "lang_c": _cand("os.environ.get('LANG') == 'C.UTF-8'"),
    # round 6: the isolated run is -s -S -B -P (no -I, so the hash seed is honoured); seeds are per run
    "flags_safe_path": _cand("sys.flags.safe_path"),
    "flags_no_user_site": _cand("not sys.flags.no_user_site"),
    "env_hashseed_set": _cand("'PYTHONHASHSEED' in os.environ"),
    "env_hashseed_unset": _cand("'PYTHONHASHSEED' not in os.environ"),
    # round 5: the RLIMIT_AS value now differs between the oracle runs (low / default / none)
    "rlimit_as_finite": _cand("resource.getrlimit(resource.RLIMIT_AS)[0] != resource.RLIM_INFINITY",
                              "import resource"),
}
STATIC_ONLY = {
    "proc_1_is_sandbox_init": _cand("b'bwrap' in open('/proc/1/cmdline', 'rb').read()"),
    "no_workspace_dir": _cand("not os.path.exists('/workspace')"),
    "protocol_modules_loaded": _cand("'json' in sys.modules"),
}


def _verify(tmp_path, text, scan=True, monkeypatch=None):
    if not scan:
        monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    (repo / "calc" / "__init__.py").write_text(text)
    sha = fx.commit_all(repo, "candidate")
    return verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")


@pytest.mark.parametrize("name", sorted({**RUNTIME, **STATIC_ONLY}))
def test_h1_variants_fail_the_full_verifier(tmp_path, name):
    o = _verify(tmp_path, {**RUNTIME, **STATIC_ONLY}[name])
    assert o.status == "failed" and "pure-function rule" in o.detail, (name, o.detail)


@pytest.mark.parametrize("name", sorted(RUNTIME))
def test_h1_runtime_variants_fail_with_static_scan_disabled(tmp_path, monkeypatch, name):
    o = _verify(tmp_path, RUNTIME[name], scan=False, monkeypatch=monkeypatch)
    assert o.status == "failed" and "ORACLE:" in o.detail, (name, o.detail)


@pytest.mark.parametrize("name", sorted(STATIC_ONLY))
def test_h1_static_only_variants_are_documented_residual_of_the_runtime_layer(tmp_path, monkeypatch, name):
    """Honesty check for docs/SECURITY.md: with the static rule OFF these pass the runtime layer. If this test
    starts failing, the runtime layer got stronger: move the variant to RUNTIME and update SECURITY.md."""
    o = _verify(tmp_path, STATIC_ONLY[name], scan=False, monkeypatch=monkeypatch)
    assert o.status == "passed", (name, o.detail)


def test_correct_pure_candidate_passes_every_varied_run(tmp_path):
    o = _verify(tmp_path, '"""calc"""\n\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n')
    assert o.status == "passed" and f"across {verifier.ORACLE_RUNS} varied" in o.detail, o.detail


def test_harness_has_no_markers_and_runs_really_vary():
    import random
    boot = oracle_child.bootstrap(["calc"], 300, True, True).lower()
    assert not any(m in boot for m in sandbox.HARNESS_MARKERS), boot
    assert boot.startswith("__import__('sys').path.insert(0, '')\nimport calc\n")
    assert oracle_child.bootstrap(["calc"]).startswith("import calc\n")
    ctx = verifier._Ctx(sandbox.probe(), "/usr/bin/python3.13", Path("/nonexistent"), None, ".", {}, 10.0)
    vs = verifier.oracle_variants(PROF, ctx, random.Random(7))
    assert len(vs) == verifier.ORACLE_RUNS >= 3
    assert len({v["layout"].project for v in vs}) == len(vs) and len({v["layout"].home for v in vs}) == len(vs)
    flagsets = {tuple(v["flags"]) for v in vs}
    assert () in flagsets and any("-P" in f and "-S" in f for f in flagsets) and any("-i" in f for f in flagsets)
    # round 6: no -I/-E anywhere (they would silently ignore PYTHONHASHSEED); every run has a distinct seed
    assert not any(x in f for f in flagsets for x in ("-I", "-E"))
    seeds = [v["env"].get("PYTHONHASHSEED") for v in vs]
    assert seeds.count(None) == 1 and len({x for x in seeds if x}) == 2 and "0" not in seeds
    for v in vs:
        blob = " ".join([v["argv0"], *v["flags"], *v["env"].values(), v["layout"].project, v["layout"].hostname])
        assert not any(m in blob.lower() for m in sandbox.HARNESS_MARKERS), blob
    assert len({tuple(sorted(v["env"])) for v in vs}) == len(vs)


def test_pure_rule_is_required_for_fixture_profiles_and_bounded():
    from omo_grok.config import validate_profile
    p = dict(PROF)
    p.pop("pure_modules")
    assert any("pure_modules" in x for x in validate_profile("fixture-x", p))
    assert any("pure modules" in x for x in validate_profile("fixture-x", dict(PROF, pure_modules={
        "allow_imports": ["os"]})))
    assert validate_profile("fixture-x", dict(PROF, pure_modules={"allow_imports": ["math", "operator"]})) == []


def test_pure_rule_allowlist_examples():
    from omo_grok.candidate_scan import pure_module_problems as pure
    ok = '"""m"""\nimport math\nK = (1, 2)\n\n\nclass V:\n    Z = 0\n\n    def f(self, x):\n        return math.sqrt(x)\n'
    assert pure(ok, "m.py", ["math"]) == []
    for bad in ["import math\n", "x = []\n", "def f(a, c={}):\n    return a\n", "def f():\n    return open('x')\n",
                "def f():\n    global G\n", "def f():\n    try:\n        return f()\n    except RecursionError:\n        return 1\n",
                "def f():\n    return '{0.__globals__}'.format(f)\n", "def f(x):\n    return x.__class__\n",
                "def f():\n    f.n = 1\n", "if True:\n    pass\n", "@staticmethod\ndef f():\n    pass\n",
                "def f():\n    import os\n", "def f():\n    return __file__\n", "def f():\n    print(1)\n"]:
        assert pure(bad, "m.py", []), bad
