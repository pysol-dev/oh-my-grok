"""Round 6 (Momus re-review #6): C2-1 argument identity, C2-2 hash-seed order, deep recursion vs stack padding,
effective per-run hash seeds, keyword/bool calls, and the bwrap namespace self-check against a fake binary."""
import os
import random
import subprocess
from pathlib import Path

import pytest

from oh_my_grok import fixture as fx
from oh_my_grok import oracles, sandbox, verifier
from oh_my_grok.config import load_verifier_profiles

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]
if not sandbox.probe().ok:
    pytest.skip("no sandbox on this host (verifier fails closed; covered elsewhere)", allow_module_level=True)

MUL = "\n\ndef mul(a, b):\n    return a * b\n"
C2_1 = {
    "momus_is_gt_1000": "def add(a, b):\n    if a is b and a > 1000:\n        return a - b\n    return a + b\n",
    "momus_is_outside_small_int_cache": ("def add(a, b):\n    if a is b and (a > 256 or a < -5):\n"
                                         "        return a - b\n    return a + b\n"),
    "is_not_variant": ("def add(a, b):\n    if a is not b or not (a > 256 or a < -5):\n        return a + b\n"
                       "    return 0\n"),
}
C2_2 = {
    "momus_set_literal_order": ("def add(a, b):\n    o = list({'alpha', 'beta', 'gamma', 'delta'})\n"
                                "    return a - b if o == sorted(o) else a + b\n"),
    "set_call": ("def add(a, b):\n    o = list(set(['alpha', 'beta', 'gamma', 'delta']))\n"
                 "    return a - b if o == sorted(o) else a + b\n"),
    "frozenset_call": ("def add(a, b):\n    o = list(frozenset(['alpha', 'beta']))\n"
                       "    return a - b if o == sorted(o) else a + b\n"),
    "set_comprehension": ("def add(a, b):\n    o = list({w for w in ('alpha', 'beta', 'gamma')})\n"
                          "    return a - b if o == sorted(o) else a + b\n"),
    "hash_builtin": "def add(a, b):\n    return a - b if hash('alpha') % 7 == 0 else a + b\n",
}


def _verify(tmp_path, text, scan=True, monkeypatch=None):
    if not scan:
        monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    (repo / "calc" / "__init__.py").write_text('"""calc"""\n\n\n' + text + MUL)
    sha = fx.commit_all(repo, "candidate")
    return verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")


@pytest.mark.parametrize("name", sorted(C2_1))
def test_c2_1_identity_variants_fail_the_pure_rule(tmp_path, name):
    o = _verify(tmp_path, C2_1[name])
    assert o.status == "failed" and "pure-function rule" in o.detail and "identity comparison" in o.detail, o.detail


@pytest.mark.parametrize("name", sorted(C2_1))
def test_c2_1_identity_variants_fail_the_aliasing_oracle_with_the_scan_off(tmp_path, monkeypatch, name):
    o = _verify(tmp_path, C2_1[name], scan=False, monkeypatch=monkeypatch)
    assert o.status == "failed" and "ORACLE:" in o.detail, o.detail


@pytest.mark.parametrize("name", sorted(C2_2))
def test_c2_2_hash_order_variants_are_rejected_statically(tmp_path, name):
    o = _verify(tmp_path, C2_2[name])
    assert o.status == "failed" and "pure-function rule" in o.detail, o.detail


def test_correct_deep_recursion_passes_every_run(tmp_path):
    """Correct code recursing ~700 deep must not be failed by the random 0..300 stack padding (limit 1000)."""
    text = ("def _r(a, b, n):\n    return a + b if n == 0 else _r(a, b, n - 1)\n\n\n"
            "def add(a, b):\n    return _r(a, b, 700)\n")
    o = _verify(tmp_path, text)
    assert o.status == "passed", o.detail


def test_keyword_and_bool_calls_are_part_of_the_contract(tmp_path, monkeypatch):
    swapped = "def add(a, b):\n    return b + a\n"  # control: still correct
    assert _verify(tmp_path / "1", swapped).status == "passed"
    renamed = "def add(x, y):\n    return x + y\n"  # keyword calls use the declared names a, b
    o = _verify(tmp_path / "2", renamed)
    assert o.status == "failed" and "ORACLE:" in o.detail, o.detail
    bool_tell = "def add(a, b):\n    return a + b + 1 if a.__class__.__name__ == 'bool' else a + b\n"
    o = _verify(tmp_path / "3", bool_tell, scan=False, monkeypatch=monkeypatch)
    assert o.status == "failed" and "ORACLE:" in o.detail, o.detail


def test_oracle_cases_include_aliased_keyword_mixed_and_bool_forms():
    cs = oracles.cases("calc-arith", 8)
    kinds = {c["kind"] for c in cs}
    assert {"fixed", "fixed-kw", "fixed-mixed", "fixed-alias", "fixed-alias-kw", "bool", "random-equal",
            "random-alias"} <= kinds
    al = [c for c in cs if c["alias"]]
    assert al and all(c["args"][0] == c["args"][1] for c in al)
    assert any(abs(c["args"][0]) > 1000 for c in al)  # beyond the small-int cache
    assert all(set(oracles.wire(c)) == {"id", "call", "args", "names", "alias"} for c in cs)


def test_each_oracle_run_has_an_effective_distinct_hash_seed():
    """The variant flags must honour PYTHONHASHSEED (-I/-E would silently ignore it)."""
    ctx = verifier._Ctx(sandbox.probe(), "/usr/bin/python3.13", Path("/nonexistent"), None, ".", {}, 10.0)
    vs = verifier.oracle_variants(PROF, ctx, random.Random(11))
    hashes = []
    for v in vs:
        env = dict(v["env"])
        r = subprocess.run(["/usr/bin/python3", *[f for f in v["flags"] if f not in ("-i", "-q")], "-c",
                            "print(hash('alpha'))"], env=env, capture_output=True, text=True, timeout=20)
        h = int(r.stdout)
        if "PYTHONHASHSEED" in env:  # pinned: identical on a second process with the same seed
            r2 = subprocess.run(["/usr/bin/python3", "-c", "print(hash('alpha'))"],
                                env={"PYTHONHASHSEED": env["PYTHONHASHSEED"]}, capture_output=True, text=True)
            assert int(r2.stdout) == h, v["name"]
        hashes.append(h)
    assert len(set(hashes)) == len(hashes)


def test_fake_bwrap_that_prints_a_version_is_rejected(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "bwrap"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "bubblewrap 0.12.0"; exit 0; fi\n'
                    'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox, "BWRAP_CANDIDATES", (str(fake),))
    pr = sandbox.probe.__wrapped__()
    assert not pr.ok and pr.mechanism == "none" and "self-check failed" in pr.detail, pr
    # a fake that only echoes canned JSON without the nonce is rejected too
    fake.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "bubblewrap 0.12.0"; exit 0; fi\n'
                    'echo \'{"pid": 2, "comm1": "bwrap", "ifaces": ["lo"], "uid_map": ["1000","0","1"], '
                    '"uid": 1000, "visible": []}\'\n')
    pr = sandbox.probe.__wrapped__()
    assert not pr.ok and "nonce" in pr.detail, pr
    assert sandbox.namespace_self_check("/usr/bin/bwrap") is None
    assert os.path.exists("/usr/bin/bwrap")
