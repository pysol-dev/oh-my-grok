"""Round 8 (Momus re-review #8, criterion 5): the large-magnitude sampling claim must be true. Each oracle run gets
its own fresh large-band draws; the catch rate is computed; a band that avoids every fixed boundary is caught by
sampling alone. Also: `__all__` is allowed in pure modules; disclosed false rejections stay as documented."""
from pathlib import Path

import pytest

from omo_grok import fixture as fx
from omo_grok import oracles, sandbox, verifier
from omo_grok.candidate_scan import pure_module_problems as pure
from omo_grok.config import load_verifier_profiles

ROOT = Path(__file__).resolve().parents[2]
PROF = load_verifier_profiles(ROOT / "config")["fixture-calc-unittest"]
MUL = "\n\ndef mul(a, b):\n    return a * b\n"
REL = "calc/__init__.py"


def _verify(tmp_path, text, mul=MUL):
    repo = tmp_path / "repo"
    base = fx.make_repo(repo)
    (repo / "calc" / "__init__.py").write_text('"""calc"""\n\n\n' + text + mul)
    sha = fx.commit_all(repo, "candidate")
    return verifier.run(repo, sha, "p", dict(PROF, protected_base=base), tmp_path / "w")


def _bits(lo, hi):
    """Bit lengths of |x| with 2**lo <= |x| < 2**hi."""
    return set(range(lo + 1, hi + 1))


# ---- formula and draw counts (no sandbox needed) ----

def test_catch_rate_formula_and_the_0_999_target_for_a_300_bit_band():
    n = oracles.LARGE_PER_RUN * 3
    assert n == 150
    assert oracles.large_band_catch_rate(300) == pytest.approx(1 - (1 - 300 / 4097) ** 150)
    assert oracles.large_band_catch_rate(300) >= 0.999
    assert round(oracles.large_band_catch_rate(50), 3) == 0.841
    assert round(oracles.large_band_catch_rate(100), 4) == 0.9754
    assert oracles.large_band_catch_rate(1000) > 1 - 1e-15


def test_run_cases_are_fresh_per_call_independent_and_large():
    a = oracles.run_cases("calc-arith", 1000)
    b = oracles.run_cases("calc-arith", 1000 + len(a))
    assert len(a) == oracles.LARGE_PER_RUN * len(oracles.ORACLES["calc-arith"])
    assert [c["id"] for c in a] == list(range(1000, 1000 + len(a)))
    assert {c["id"] for c in a}.isdisjoint(c["id"] for c in b)
    assert {tuple(c["args"]) for c in a}.isdisjoint(tuple(c["args"]) for c in b)
    for c in a:
        assert all(1 <= abs(x).bit_length() <= oracles.LARGE_EXP_MAX + 1 for x in c["args"])
        assert len(str(abs(c["expect"]))) < 4300
    # exponent uniform in 0..4096: over many draws, every 512-wide slice of bit lengths is hit
    bits = [abs(oracles.rand_large()).bit_length() for _ in range(4000)]
    assert all(any(lo < b <= lo + 512 for b in bits) for lo in range(0, 4096, 512))


SAMPLING_BANDS = {  # exponent bands that contain NO fixed boundary value and no grid value
    "a_2500_2800": ("a", 2500, 2800),
    "b_3100_3400": ("b", 3100, 3400),
}


@pytest.mark.parametrize("name", sorted(SAMPLING_BANDS))
def test_sampling_bands_avoid_every_fixed_value(name):
    _, lo, hi = SAMPLING_BANDS[name]
    fixed = {abs(x).bit_length() for x in oracles.BOUNDARY_INTS} | {abs(x).bit_length() for x in oracles.GRID}
    assert fixed.isdisjoint(_bits(lo, hi))
    shared = [x for c in oracles.cases("calc-arith", 48) if c["kind"] not in ("random", "random-equal",
                                                                             "random-alias") for x in c["args"]]
    assert not any(lo + 1 <= abs(x).bit_length() <= hi for x in shared if not isinstance(x, bool))


needs_sandbox = pytest.mark.skipif(not sandbox.probe().ok, reason="no sandbox on this host")


@needs_sandbox
@pytest.mark.parametrize("name", sorted(SAMPLING_BANDS))
def test_wrong_only_in_a_300_bit_band_fails_by_sampling_alone(tmp_path, name):
    arg, lo, hi = SAMPLING_BANDS[name]
    text = f"def add(a, b):\n    return a - b if 2 ** {lo} <= abs({arg}) < 2 ** {hi} else a + b\n"
    assert pure(text + MUL, REL, []) == []
    o = _verify(tmp_path, text)
    assert o.status == "failed" and "ORACLE:" in o.detail and "wrong value" in o.detail, o.detail


@needs_sandbox
def test_each_run_gets_its_own_large_draws(tmp_path, monkeypatch):
    seen = []
    real = verifier._oracle_session

    def spy(ctx, v, cases, mods):
        seen.append(list(cases))
        return real(ctx, v, cases, mods)
    monkeypatch.setattr(verifier, "_oracle_session", spy)
    o = _verify(tmp_path, "def add(a, b):\n    return a + b\n")
    assert o.status == "passed", o.detail
    assert len(seen) == verifier.ORACLE_RUNS
    own = [{c["id"]: tuple(c["args"]) for c in run if c["kind"] == "run-large"} for run in seen]
    shared = [{c["id"] for c in run if c["kind"] != "run-large"} for run in seen]
    assert shared[0] == shared[1] == shared[2]
    for i in range(3):
        assert len(own[i]) == oracles.LARGE_PER_RUN * 2
        for j in range(i + 1, 3):
            assert set(own[i]).isdisjoint(own[j])
            assert set(own[i].values()).isdisjoint(own[j].values())
    assert f"{oracles.LARGE_PER_RUN} fresh large-magnitude draws" in o.detail


# ---- __all__ (allowed since round 8) and disclosed false rejections ----

ALL_OK = ['__all__ = ("add", "mul")\n', '__all__ = ["add", "mul"]\n', '__all__ = ()\n']
ALL_BAD = ['__all__ = ("add", 1)\n', '__all__ = names\n', '__all__ = __all__ = ("a",)\n',
           '__all__: tuple = ("add",)\n', 'X = __all__\n', '__all__ += ("x",)\n', '__doc__ = "x"\n',
           '__all__ = ("add",) + ("mul",)\n', 'def f():\n    return __all__\n']


@pytest.mark.parametrize("tail", ALL_OK)
def test_dunder_all_of_str_constants_is_allowed(tail):
    assert pure("def add(a, b):\n    return a + b\n" + MUL + tail, REL, []) == []


@pytest.mark.parametrize("tail", ALL_BAD)
def test_other_dunder_all_forms_stay_rejected(tail):
    assert pure("def add(a, b):\n    return a + b\n" + MUL + tail, REL, [])


@needs_sandbox
def test_real_fix_with_dunder_all_passes_the_verifier(tmp_path):
    o = _verify(tmp_path, '__all__ = ("add", "mul")\n\n\ndef add(a, b):\n    return a + b\n')
    assert o.status == "passed", o.detail


DISCLOSED_FALSE_REJECTIONS = {  # correct code the rule rejects (SECURITY.md "False rejections")
    "f_string": 'def add(a, b):\n    s = f"{a}"\n    return a + b\n',
    "main_guard": 'def add(a, b):\n    return a + b\n\n\nif __name__ == "__main__":\n    pass\n',
    "method_call": 'def add(a, b):\n    return a.__add__(b)\n',
}


@pytest.mark.parametrize("name", sorted(DISCLOSED_FALSE_REJECTIONS))
def test_disclosed_false_rejections_are_still_rejected(name):
    assert pure(DISCLOSED_FALSE_REJECTIONS[name] + MUL, REL, [])


@needs_sandbox
def test_unconditional_recursion_970_deep_passes(tmp_path):
    text = ("def _r(a, b, n):\n    return a + b if n == 0 else _r(a, b, n - 1)\n\n\n"
            "def add(a, b):\n    return _r(a, b, 970)\n")
    o = _verify(tmp_path, text)
    assert o.status == "passed", o.detail
