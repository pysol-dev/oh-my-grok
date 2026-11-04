"""Round 7 (Momus re-review #7): the fixture pure rule is an ALLOWLIST (C2-3 dict-view set ops and every earlier
attack are outside it), C2-4 (recursion headroom must equal real use), large-magnitude oracle coverage."""
import subprocess
from pathlib import Path

import pytest

import test_harness_detection as h1
import test_round5_probes as r5
import test_round6_probes as r6
from omo_grok import fixture as fx
from omo_grok import oracle_child, oracles, sandbox, verifier
from omo_grok.candidate_scan import pure_allowlist_problems as allow_only
from omo_grok.candidate_scan import pure_module_problems as pure
from omo_grok.config import load_verifier_profiles

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]
if not sandbox.probe().ok:
    pytest.skip("no sandbox on this host (verifier fails closed; covered elsewhere)", allow_module_level=True)
MUL = "\n\ndef mul(a, b):\n    return a * b\n"
LOCAL = frozenset({"calc/__init__.py", "calc/helper.py"})

C2_3 = {
    "momus_keys_or_empty_tuple": ("def add(a, b):\n    o = list({'alpha': 0, 'beta': 0, 'gamma': 0, 'delta': 0}.keys() | ())\n"
                                  "    return a - b if o == sorted(o) else a + b\n"),
    "keys_and_keys": ("def add(a, b):\n    d = {'alpha': 0, 'beta': 0, 'gamma': 0, 'delta': 0}\n"
                      "    o = list(d.keys() & d.keys())\n    return a - b if o == sorted(o) else a + b\n"),
    "keys_xor_empty_dict": ("def add(a, b):\n    d = {'alpha': 0, 'beta': 0, 'gamma': 0, 'delta': 0}\n"
                            "    o = list(d.keys() ^ {})\n    return a - b if o == sorted(o) else a + b\n"),
    "items_or": ("def add(a, b):\n    d = {'alpha': 0, 'beta': 1}\n    o = list(d.items() | ())\n"
                 "    return a - b if o == sorted(o) else a + b\n"),
}

ACCEPTED = {
    "calc_real_fix": '"""calc"""\n\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n',
    "calc_fixture_base": (ROOT / "fixtures/calc-src/calc/__init__.py").read_text(),
    "recursion": "def _r(a, b, n):\n    return a + b if n == 0 else _r(a, b, n - 1)\n\n\ndef add(a, b):\n    return _r(a, b, 700)\n",
    "for_range": "def f(n):\n    t = 0\n    for i in range(n):\n        t += i\n    return t\n",
    "while_break_continue": ("def f(n):\n    while n > 0:\n        n -= 1\n        if n == 5:\n            continue\n"
                             "        if n < 2:\n            break\n    return n\n"),
    "tuple_unpack": "def f(a, b):\n    q, r = divmod(a, b)\n    return (q, r)[0] + [q, r][1]\n",
    "ifexp_boolop": "def f(a, b):\n    return a if a > b and not (a == 0 or b == 0) else b\n",
    "chained_compare_in": "def f(a):\n    return 0 <= a < 10 and a in (1, 2, 3) and a not in [4]\n",
    "builtins": ("def f(a, b):\n    return abs(a) + min(a, b) + max(a, b) + int(1.5) + len('ab') + pow(a, 2, 7)"
                 " + round(2.5) + sum([a, b]) + bool(a) + float(b) * 0\n"),
    "isinstance_raise": ("def f(a):\n    if not isinstance(a, int):\n        raise TypeError('int please')\n"
                         "    if a < 0:\n        raise ValueError\n    return a\n"),
    "zero_division_raise": "def f(a, b):\n    if b == 0:\n        raise ZeroDivisionError('b')\n    return a // b\n",
    "docstrings_and_consts": '"""m"""\nK = 3\nT = (1, -2, "x", None, True, 2.5)\n\n\ndef f(a):\n    """doc"""\n    return a * K + T[0]\n',
    "annotations_defaults": "def f(a: int, b: int = 2) -> int:\n    return a + b\n",
    "math_module": "import math\n\n\ndef f(a):\n    return math.isqrt(abs(a)) + math.gcd(a, 6) + int(math.pi)\n",
    "from_math": "from math import gcd\n\n\ndef f(a, b):\n    return gcd(a, b)\n",
    "slices": "def f(a):\n    t = (1, 2, 3, 4)\n    return t[1:3][0] + t[::-1][0] + a\n",
    "bitops": "def f(a, b):\n    return (a & b) | (a ^ b) ^ ~a << 2 >> 1\n",
    "future_annotations": "from __future__ import annotations\n\n\ndef f(a: int) -> int:\n    return a\n",
    "relative_helper": "from . import helper\n\n\ndef f(a):\n    return helper.g(a)\n",
    "pass_and_bare_return": "def f(a):\n    if a:\n        pass\n    return\n",
}
IMPORTS = {"math_module": ["math"], "from_math": ["math"]}

REJECTED = {  # construct -> a few words of the expected reason
    "dict_literal": "def f():\n    return {'a': 1}\n",
    "dict_comp": "def f():\n    return {k: 0 for k in range(3)}\n",
    "set_literal": "def f():\n    return {1, 2}\n",
    "list_comp": "def f():\n    return [i for i in range(3)]\n",
    "generator_exp": "def f():\n    return sum(i for i in range(3))\n",
    "lambda": "def f():\n    g = lambda x: x\n    return g(1)\n",
    "method_call_on_str": "def f(s):\n    return s.upper()\n",
    "method_on_list": "def f():\n    t = [3, 1]\n    t.sort()\n    return t\n",
    "attribute_of_value": "def f(a):\n    return a.real\n",
    "dict_builtin": "def f():\n    return dict(a=1)\n",
    "sorted_builtin": "def f(t):\n    return sorted(t)\n",
    "repr_builtin": "def f():\n    return repr(f)\n",
    "type_builtin": "def f(a):\n    return type(a)\n",
    "is_compare": "def f(a, b):\n    return a is b\n",
    "is_none": "def f(a):\n    return a is None\n",
    "fstring": "def f(a):\n    return f'{a!r}'\n",
    "starred_call": "def f(t):\n    return max(*t)\n",
    "kwargs_call": "def f(d):\n    return max(1, **d)\n",
    "varargs_param": "def f(*a):\n    return 0\n",
    "kwonly_param": "def f(a, *, b):\n    return b\n",
    "kwargs_param": "def f(**k):\n    return 0\n",
    "nested_def": "def f():\n    def g():\n        return 1\n    return g()\n",
    "class_def": "class C:\n    pass\n",
    "decorator": "def d(x):\n    return x\n\n\n@d\ndef f():\n    return 1\n",
    "expr_statement_call": "def f():\n    max(1, 2)\n    return 1\n",
    "assert_stmt": "def f(a):\n    assert a\n    return a\n",
    "for_over_tuple": "def f():\n    for x in (1, 2):\n        pass\n    return 0\n",
    "for_over_name": "def f(t):\n    for x in t:\n        pass\n    return 0\n",
    "item_assignment": "def f():\n    t = [0]\n    t[0] = 1\n    return t\n",
    "attribute_assignment": "def f():\n    f.n = 1\n",
    "del_stmt": "def f(a):\n    del a\n",
    "raise_other": "def f():\n    raise RuntimeError('x')\n",
    "raise_from": "def f():\n    raise ValueError('x') from None\n",
    "bytes_constant": "def f():\n    return b'x'\n",
    "complex_constant": "def f():\n    return 1j\n",
    "ellipsis_constant": "def f():\n    return ...\n",
    "walrus": "def f(a):\n    return (b := a)\n",
    "match_stmt": "def f(a):\n    match a:\n        case 1:\n            return 1\n    return 0\n",
    "matmult": "def f(a, b):\n    return a @ b\n",
    "module_level_call": "X = max(1, 2)\n",
    "module_level_list": "X = [1]\n",
    "import_os": "import os\n",
    "math_unlisted_attr": "import math\n\n\ndef f():\n    return math.__dict__\n",
    "operator_attrgetter": "import operator\n\n\ndef f(a):\n    return operator.attrgetter('real')(a)\n",
    "call_of_call": "def g(a):\n    return g\n\n\ndef f(a):\n    return g(a)(a)\n",
    "subscript_callee": "def f(a):\n    return (max, min)[0](a, 1)\n",
    "shadow_range": "def f(range):\n    for i in range(3):\n        pass\n    return 0\n",
    "global_stmt": "def f():\n    global G\n    return 0\n",
    "yield": "def f():\n    yield 1\n",
    "try": "def f():\n    try:\n        return 1\n    finally:\n        return 2\n",
    "with": "def f(c):\n    with c:\n        return 1\n",
}
REJECTED.update({f"c2_3_{k}": v for k, v in C2_3.items()})


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_allowlist_accepts(name):
    assert pure(ACCEPTED[name], "calc/__init__.py", IMPORTS.get(name, []), LOCAL) == [], name


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_allowlist_layer_alone_rejects(name):
    imports = ["math", "operator"] if "math" in name or "operator" in name else []
    assert allow_only(REJECTED[name], "calc/__init__.py", imports, LOCAL), name
    assert pure(REJECTED[name], "calc/__init__.py", imports, LOCAL), name


def _earlier_suite():
    out = {}
    for k, v in {**h1.RUNTIME, **h1.STATIC_ONLY}.items():
        out[f"h1_{k}"] = {"calc/__init__.py": v}
    for k, files in r5.CASES.items():
        out[f"r5_{k}"] = files
    out["r5_HDIST"] = None  # wrong values, not a sensing construct: the oracle's job (allowlist accepts it)
    for k, v in r5.AUDIT.items():
        out[f"audit_{k}"] = {"calc/__init__.py": v}
    for k, v in {**r6.C2_1, **r6.C2_2}.items():
        out[f"r6_{k}"] = {"calc/__init__.py": v}
    return {k: v for k, v in out.items() if v is not None}


EARLIER = _earlier_suite()
# relative-import resolution is checked by the import layer (the allowlist only allows the syntax)
IMPORT_LAYER_ONLY = {"audit_relative_outside_editable", "audit_relative_unknown_module", "audit_relative_star"}


@pytest.mark.parametrize("name", sorted(EARLIER))
def test_entire_earlier_attack_suite_is_outside_the_allowlist(name):
    files = EARLIER[name]
    assert any(pure(t, rel, [], LOCAL) for rel, t in files.items()), name
    if name not in IMPORT_LAYER_ONLY:
        assert any(allow_only(t, rel, [], LOCAL) for rel, t in files.items()), name


def _verify(tmp_path, text, scan=True, monkeypatch=None):
    if not scan:
        monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    (repo / "calc" / "__init__.py").write_text('"""calc"""\n\n\n' + text + MUL)
    sha = fx.commit_all(repo, "candidate")
    return verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")


@pytest.mark.parametrize("name", sorted(C2_3))
def test_c2_3_dict_view_variants_fail_the_full_verifier(tmp_path, name):
    o = _verify(tmp_path, C2_3[name])
    assert o.status == "failed" and "[allowlist]" in o.detail, o.detail


C2_4 = ("def _d(n):\n    return 0 if n == 0 else _d(n - 1)\n\n\n"
        "def add(a, b):\n    if abs(a) > 100:\n        t = _d(1010)\n    return a + b\n")


def test_c2_4_passes_the_allowlist_but_fails_the_oracle(tmp_path, monkeypatch):
    assert pure(C2_4 + MUL, "calc/__init__.py", []) == []
    o = _verify(tmp_path / "on", C2_4)
    assert o.status == "failed" and "ORACLE:" in o.detail, o.detail
    o = _verify(tmp_path / "off", C2_4, scan=False, monkeypatch=monkeypatch)
    assert o.status == "failed" and "ORACLE:" in o.detail, o.detail


def test_recursion_headroom_in_every_run_equals_a_top_level_call(tmp_path):
    """C2-4: the oracle child must allow exactly the recursion depth a script calling calc.add at top level gets:
    the real maximum passes in every flag set and padding, one more fails."""
    pkg = tmp_path / "calc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def _d(n):\n    return 0 if n == 0 else _d(n - 1)\n\n\n"
                                     "def add(a, b):\n    return a + b + _d(b)\n")

    def real_ok(k):
        return subprocess.run(["/usr/bin/python3", "-c", f"import calc\ncalc.add(0, {k})"], cwd=tmp_path,
                              capture_output=True).returncode == 0
    lo, hi = 1, 5000
    while lo < hi:
        m = (lo + hi + 1) // 2
        lo, hi = (m, hi) if real_ok(m) else (lo, m - 1)
    kmax = lo
    assert 900 < kmax < 1000
    import json
    for pad in (0, 150, 300):
        for flags in ([], ["-q", "-i"], ["-s", "-S", "-B", "-P"]):
            boot = oracle_child.bootstrap(["calc"], pad, "-i" in flags, "-P" in flags)
            reqs = "".join(json.dumps({"id": i, "call": "calc.add", "args": [0, k]}) + "\n"
                           for i, k in enumerate([kmax, kmax + 1] * 15)) + '{"end": true}\n'
            r = subprocess.run(["/usr/bin/python3", *flags, "-c", boot], cwd=tmp_path, input=reqs,
                               capture_output=True, text=True, timeout=60)
            out = [json.loads(x) for x in r.stdout.splitlines()][1:]
            assert len(out) == 30, (pad, flags, r.stderr[-300:])
            assert all("value" in o for o in out if o["id"] % 2 == 0), (pad, flags)
            assert all(o.get("error") == "RecursionError" for o in out if o["id"] % 2 == 1), (pad, flags)


def test_plain_run_calls_at_the_stock_limit_without_padding():
    import random
    ctx = verifier._Ctx(sandbox.probe(), "/usr/bin/python3.13", Path("/nonexistent"), None, ".", {}, 10.0)
    vs = verifier.oracle_variants(PROF, ctx, random.Random(3))
    assert any(v["pad"] == 0 for v in vs)
    boot = oracle_child.bootstrap(["calc"], 0)
    assert "+ pad_max" not in boot and "50" not in boot  # no blanket headroom


LARGE = {
    "wrong_above_2_300_except_2_1000": ("def add(a, b):\n    if abs(a) > 2 ** 300 and abs(a) != 2 ** 1000:\n"
                                        "        return a + b + 1\n    return a + b\n"),
    "wrong_between_2_2049_and_2_4095": ("def add(a, b):\n    if 2 ** 2049 < abs(b) < 2 ** 4095:\n"
                                        "        return a + b - 1\n    return a + b\n"),
    "wrong_only_above_2_4000": "def add(a, b):\n    return a + b + 1 if abs(a) > 2 ** 4000 else a + b\n",
}


@pytest.mark.parametrize("name", sorted(LARGE))
def test_large_magnitude_wrong_candidates_fail(tmp_path, name):
    o = _verify(tmp_path, LARGE[name])
    assert o.status == "failed" and "ORACLE:" in o.detail, o.detail


def test_cases_span_magnitudes_up_to_2_4096():
    assert {2 ** 300 + 1, 2 ** 4096, -(2 ** 4096), 2 ** 1000 + 1} <= set(oracles.BOUNDARY_INTS)
    draws = [oracles.rand_int() for _ in range(3000)]
    bits = [abs(x).bit_length() for x in draws]
    assert max(bits) <= oracles.LARGE_EXP_MAX + 1
    assert any(300 < b <= 1000 for b in bits) and any(1000 < b <= 2000 for b in bits) and \
        any(3000 < b for b in bits)
    assert any(x < 0 and abs(x).bit_length() > 1000 for x in draws)
    # the JSON protocol must carry every expected value (default 4300-digit int<->str limit)
    assert max(len(str(abs(c["expect"]))) for c in oracles.cases("calc-arith", 48)) < 4300
