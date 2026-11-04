"""Momus re-review #5 (round 5): HMEM (memory-limit sensing swallowed by `return` in `finally`), HMEM through a
relative-import helper, HDIST (wrong only on small inputs the random draws rarely hit), a recursion-depth try/finally
probe, and the round-5 audit of other exception-swallowing / sandbox-sensing constructs.

Layers exercised: the pure-function rule (try banned, bytes dropped, dunder hooks banned, relative imports resolved
against the scanned editable set), the oracle's exhaustive small grid + boundaries, and the 3 oracle runs with
DIFFERENT address-space limits (low / default / none) whose results must agree."""
from pathlib import Path

import pytest

from omo_grok import fixture as fx
from omo_grok import oracles, sandbox, verifier
from omo_grok.candidate_scan import pure_module_problems as pure
from omo_grok.config import load_verifier_profiles

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]
if not sandbox.probe().ok:
    pytest.skip("no sandbox on this host (verifier fails closed; covered elsewhere)", allow_module_level=True)

MUL = "\n\ndef mul(a, b):\n    return a * b\n"
# exact Momus HMEM payload
HMEM = ("def _p():\n  ok=False\n  try: bytes(5<<30); ok=True\n  finally: return ok\n\n\n"
        "def add(a, b):\n    return a-b if _p() else a+b\n" + MUL)
HMEM_HELPER = "def _p():\n  ok=False\n  try: bytes(5<<30); ok=True\n  finally: return ok\n\n\ndef probe():\n    return _p()\n"
HMEM_REL_INIT = "from . import helper\n\n\ndef add(a, b):\n    return a-b if helper.probe() else a+b\n" + MUL
HMEM_REL_FROM = "from .helper import probe\n\n\ndef add(a, b):\n    return a-b if probe() else a+b\n" + MUL
# HDIST: add wrong for |a|,|b| <= 10 except the unittest pairs (2,3), (-4,10), (0,0)
HDIST = ("def add(a, b):\n    if -10 <= a <= 10 and -10 <= b <= 10 and (a, b) not in ((2, 3), (-4, 10), (0, 0)):\n"
         "        return a + b + 1\n    return a + b\n" + MUL)
HREC = ("def _d(n):\n    return 0 if n == 0 else 1 + _d(n - 1)\n\n\n"
        "def _p():\n    ok = False\n    try:\n        _d(900)\n        ok = True\n    finally:\n        return ok\n\n\n"
        "def add(a, b):\n    return a + b if _p() else a - b\n" + MUL)


def _verify(tmp_path, files: dict, scan=True, monkeypatch=None):
    if not scan:
        monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    sha = fx.commit_all(repo, "candidate")
    return verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")


CASES = {
    "HMEM": {"calc/__init__.py": HMEM},
    "HMEM_relative_import_module": {"calc/__init__.py": HMEM_REL_INIT, "calc/helper.py": HMEM_HELPER},
    "HMEM_relative_from_import": {"calc/__init__.py": HMEM_REL_FROM, "calc/helper.py": HMEM_HELPER},
    "HREC_recursion_try_finally": {"calc/__init__.py": HREC},
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_probe_payloads_fail_the_full_verifier(tmp_path, name):
    o = _verify(tmp_path, CASES[name])
    assert o.status == "failed" and "pure-function rule" in o.detail and "try statement" in o.detail, (name, o.detail)
    assert o.sandbox == "none"  # rejected before any candidate code ran


@pytest.mark.parametrize("name", ["HMEM", "HMEM_relative_import_module", "HMEM_relative_from_import"])
def test_hmem_is_also_caught_by_resource_variation_with_scan_off(tmp_path, monkeypatch, name):
    """The run without an address-space limit can allocate 5 GiB (lazily, calloc), the others cannot: the probe gives
    a different answer in that run, so the candidate returns a-b there and the oracle fails."""
    o = _verify(tmp_path, CASES[name], scan=False, monkeypatch=monkeypatch)
    assert o.status == "failed" and "ORACLE:" in o.detail, (name, o.detail)


def test_hdist_fails_full_verifier_on_the_exhaustive_grid(tmp_path):
    o = _verify(tmp_path, {"calc/__init__.py": HDIST})
    assert "pure-function rule" not in o.detail  # it IS pure; only the oracle can catch it
    assert o.status == "failed" and "ORACLE:" in o.detail and "calc.add" in o.detail, o.detail


@pytest.mark.parametrize("_", range(5))
def test_hdist_never_passes_repeated(tmp_path, _):
    o = _verify(tmp_path, {"calc/__init__.py": HDIST})
    assert o.status == "failed", o.detail


def test_oracle_cases_cover_grid_and_boundaries():
    cs = oracles.cases("calc-arith", 4)
    for call in ("calc.add", "calc.mul"):
        got = {tuple(c["args"]) for c in cs if c["call"] == call}
        assert all((a, b) in got for a in range(-16, 17) for b in range(-16, 17)), call
        for v in (0, 1, -1, 2 ** 63 - 1, -(2 ** 63), 2 ** 64, 2 ** 53 + 1, -(2 ** 1000)):
            assert (v, v) in got and (v, -1) in got and (-1, v) in got, (call, v)
        assert sum(1 for c in cs if c["call"] == call and c["kind"] == "random") == 4
    assert len({c["id"] for c in cs}) == len(cs)


def test_oracle_domain_filter_is_honoured(monkeypatch):
    fn = oracles.OracleFn("calc.div", 2, oracles.rand_int, lambda a, b: a // b, int, domain=lambda a, b: b != 0)
    monkeypatch.setitem(oracles.ORACLES, "div-test", [fn])
    cs = oracles.cases("div-test", 20)
    assert cs and all(c["args"][1] != 0 for c in cs)


def test_oracle_runs_differ_in_address_space_limit_and_legit_candidate_passes(tmp_path):
    import random
    ctx = verifier._Ctx(sandbox.probe(), "/usr/bin/python3.13", Path("/nonexistent"), None, ".", {}, 10.0)
    for seed in range(5):
        lims = [v["as_limit"] for v in verifier.oracle_variants(PROF, ctx, random.Random(seed))]
        assert None in lims and sandbox.RLIMITS[sandbox.resource.RLIMIT_AS] in lims
        assert any(x is not None and x <= 512 << 20 for x in lims), lims
    o = _verify(tmp_path, {"calc/__init__.py": '"""calc"""\n\n\ndef add(a, b):\n    return a + b\n' + MUL})
    assert o.status == "passed" and "different memory limits" in o.detail, o.detail


# --- round-5 audit: other ways fixture code could sense the sandbox without the forbidden names ------------------
AUDIT = {
    "try_finally_return": "def f():\n    try:\n        return 1\n    finally:\n        return 2\n",
    "try_except_valueerror": "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        return 0\n",
    "try_else": "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n    else:\n        return 1\n",
    "generator_finally": "def g():\n    try:\n        yield 1\n    finally:\n        pass\n",
    "with_context_manager": ("class C:\n    def __enter__(self):\n        return self\n\n"
                             "    def __exit__(self, *a):\n        return True\n\n\n"
                             "def f():\n    with C():\n        bytes(5 << 30)\n    return 1\n"),
    "exit_hook_only": "class C:\n    def __exit__(self, *a):\n        return True\n",
    "del_hook": "class C:\n    def __del__(self):\n        pass\n",
    "del_hook_assigned": "def _d(self):\n    pass\n\n\nclass C:\n    __del__ = _d\n",
    "index_hook": "class C:\n    def __index__(self):\n        return 1\n",
    "bytes_builtin": "def f():\n    return len(bytes(5 << 30))\n",
    "bytearray_builtin": "def f():\n    return len(bytearray(5 << 30))\n",
    "memoryview_builtin": "def f(x):\n    return memoryview(x)\n",
    "gi_frame_filename": "def g():\n    yield 1\n\n\ndef f():\n    return g().gi_frame.f_code.co_filename\n",
    "generator_throw": "def f(g):\n    return g.throw(ValueError)\n",
    "generator_close": "def f(g):\n    g.close()\n",
    "mro_walk": "def f():\n    return int.mro()\n",
    "relative_outside_editable": "from .. import tests\n",
    "relative_unknown_module": "from .nothere import x\n",
    "relative_star": "from .helper import *\n",
    "absolute_self_import": "import calc.helper\n",
    "except_star": "def f():\n    try:\n        pass\n    except* ValueError:\n        pass\n",
}


@pytest.mark.parametrize("name", sorted(AUDIT))
def test_audit_constructs_rejected_by_pure_rule(name):
    local = frozenset({"calc/__init__.py", "calc/helper.py"})
    assert pure(AUDIT[name], "calc/__init__.py", [], local), name


def test_relative_imports_of_scanned_modules_are_allowed():
    local = frozenset({"calc/__init__.py", "calc/helper.py", "calc/sub/__init__.py", "calc/sub/x.py"})
    assert pure("from . import helper\n", "calc/__init__.py", [], local) == []
    assert pure("from .helper import probe\n", "calc/__init__.py", [], local) == []
    assert pure("from .. import helper\n", "calc/sub/x.py", [], local) == []
    assert pure("from .x import y\n", "calc/sub/__init__.py", [], local) == []
    assert pure("from . import helper\n", "calc/__init__.py", [], frozenset({"calc/__init__.py"}))


@pytest.mark.parametrize("name", ["with_context_manager", "del_hook", "gi_frame_filename", "generator_finally"])
def test_audit_constructs_fail_the_full_verifier(tmp_path, name):
    o = _verify(tmp_path, {"calc/__init__.py": AUDIT[name] + "\n\ndef add(a, b):\n    return a + b\n" + MUL})
    assert o.status == "failed" and "pure-function rule" in o.detail, (name, o.detail)


def test_relative_import_of_protected_unscanned_module_fails_full_verifier(tmp_path):
    o = _verify(tmp_path, {"calc/__init__.py": "from .. import tests\n\n\ndef add(a, b):\n    return a + b\n" + MUL})
    assert o.status == "failed" and "relative import" in o.detail, o.detail


def test_unchanged_editable_files_are_also_scanned(tmp_path):
    """A helper planted in the base revision (not part of the diff) is still scanned."""
    repo = tmp_path / "repo"
    fx.make_repo(repo)
    (repo / "calc" / "helper.py").write_text(HMEM_HELPER)
    base = fx.commit_all(repo, "base with helper")
    (repo / "calc" / "__init__.py").write_text(HMEM_REL_INIT)
    sha = fx.commit_all(repo, "candidate")
    o = verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")
    assert o.status == "failed" and "calc/helper.py" in o.detail and "try statement" in o.detail, o.detail
