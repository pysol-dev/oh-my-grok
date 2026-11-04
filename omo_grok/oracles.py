"""Trusted oracles: expected behaviour defined in the orchestrator's own code, never in the candidate tree.

The verifier parent (which never imports candidate code) builds the cases for every verification from three parts:
  1. an EXHAUSTIVE small grid (every argument tuple in [-16, 16]^arity, filtered by the function's domain),
  2. fixed BOUNDARY values (0, +-1, +-2, sign mixes, 2**31/2**32/2**53/2**63/2**64 neighbourhoods, +-10**18,
     +-2**200, +-2**300(+1), +-2**1000(+-1), 2**2048+c, 3**1500, +-2**4096(+-1)) combined pairwise,
  3. FRESH random draws from the OS CSPRNG across magnitude bands, including a band of |x| ~ 2**k with k uniform in
     0..4096 and a random mantissa.
Magnitude ceiling (stated): nothing above about 2**4097 is ever sent. Products of two such values stay below
Python's default 4300-digit int<->str conversion limit, which the JSON protocol needs.
It sends them one at a time, in a shuffled order, to an untrusted child that calls the candidate, and compares the raw
JSON values it gets back with what the oracle computes here. A candidate cannot pass without returning the right value
on the whole small grid, on every boundary pair, and on inputs it could not know in advance.

Coverage limit (stated, not hidden): outside the grid and the boundary set the oracle only samples. A function that is
wrong on a sparse set of large inputs that are neither boundaries nor likely random draws (for example exactly one
specific 40-digit value) is not detected by this oracle; independent code review and QA remain the backstop."""
from __future__ import annotations

import itertools
import secrets
from dataclasses import dataclass
from typing import Any, Callable

_rng = secrets.SystemRandom()


GRID = range(-16, 17)  # exhaustive small grid per argument
LARGE_EXP_MAX = 4096   # random large-magnitude band: |x| ~ 2**k, k in 0..4096


def _boundary_ints() -> list[int]:
    """Zero, +-1, +-2, the edges of the small grid, and both sides of the common machine-int / float-exact limits
    (2**31, 2**32, 2**53, 2**63, 2**64), plus 10**18 and arbitrary-precision magnitudes, with both signs."""
    pos = {1, 2, 16, 17, 2 ** 31 - 1, 2 ** 31, 2 ** 32, 2 ** 53 + 1, 2 ** 63 - 1, 2 ** 63, 2 ** 64, 10 ** 18,
           2 ** 200, 2 ** 1000,
           # round 7: large magnitudes up to the 2**4096 ceiling, both sides of each power, plus non-powers
           2 ** 300, 2 ** 300 + 1, 2 ** 1000 - 1, 2 ** 1000 + 1, 2 ** 2048 + 12345, 3 ** 1500,
           2 ** 4096 - 1, 2 ** 4096, 2 ** 4096 + 1}
    return sorted({0} | pos | {-x for x in pos})


BOUNDARY_INTS = _boundary_ints()


def rand_int() -> int:
    """Mix of magnitudes: small, medium, 64-bit-ish and arbitrary precision, both signs."""
    band = _rng.randrange(6)
    if band == 5:  # |x| in [2**k, 2**(k+1)) for k uniform in 0..4096, random mantissa, random sign
        k = _rng.randrange(0, LARGE_EXP_MAX + 1)
        x = (1 << k) | _rng.getrandbits(k) if k else 1
        return x if _rng.randrange(2) else -x
    if band == 0:
        return _rng.randint(-100, 100)
    if band == 4:
        return _rng.randint(-1000, 1000)
    if band == 1:
        return _rng.randint(-(10 ** 12), 10 ** 12)
    if band == 2:
        return _rng.randint(-(2 ** 63), 2 ** 63)
    return _rng.randint(-(2 ** 200), 2 ** 200)


@dataclass(frozen=True)
class OracleFn:
    call: str                      # "module.function" in the candidate
    arity: int
    gen: Callable[[], Any]         # draws one argument
    expect: Callable[..., Any]     # trusted reference implementation
    result_type: type              # exact JSON-decoded type required (bool is never accepted for int)
    grid: tuple = tuple(GRID)      # exhaustive small values per argument
    boundaries: tuple = tuple(BOUNDARY_INTS)  # combined pairwise (all tuples for arity <= 2)
    domain: Callable[..., bool] = lambda *a: True  # e.g. lambda a, b: b != 0 for a divisor
    params: tuple = ("a", "b")     # parameter names: the oracle also calls by keyword (part of the contract)
    accepts_bool: bool = False     # bool is in the declared domain (bool is an int subtype): send bool cases too


ORACLES: dict[str, list[OracleFn]] = {
    "calc-arith": [
        OracleFn("calc.add", 2, rand_int, lambda a, b: a + b, int, accepts_bool=True),
        OracleFn("calc.mul", 2, rand_int, lambda a, b: a * b, int, accepts_bool=True),
    ],
}


def _fixed_args(fn: OracleFn) -> list[tuple]:
    """Exhaustive grid + boundary combinations, domain-filtered, deduplicated, deterministic order."""
    seen: set = set()
    out = []
    groups = [itertools.product(fn.grid, repeat=fn.arity)]
    if fn.arity <= 2:
        groups.append(itertools.product(fn.boundaries, repeat=fn.arity))
    if fn.arity == 2:  # large magnitudes against small values, both orders (sign mixes)
        groups.append(t for b in fn.boundaries for g in (0, 1, -1, 2, -2, 16, -16) for t in ((b, g), (g, b)))
    for tuples in groups:
        for t in tuples:
            if t not in seen and fn.domain(*t):
                seen.add(t)
                out.append(t)
    return out


def _case(fn: OracleFn, args, kind: str, form: str = "pos", alias: bool = False) -> dict:
    """form: pos (all positional) | kw (all by keyword) | mixed (first positional, rest by keyword).
    alias: the child passes the SAME object for every argument equal to the first one (as `x = ...; f(x, x)` would);
    otherwise each argument is a fresh object from the JSON decoder."""
    args = list(args)
    n = len(args)
    names = {"pos": [""] * n, "kw": list(fn.params[:n]), "mixed": [""] + list(fn.params[1:n])}[form]
    al = [[0, k] for k in range(1, n) if alias and type(args[k]) is type(args[0]) and args[k] == args[0]]
    return {"call": fn.call, "args": args, "names": names, "alias": al, "expect": fn.expect(*args),
            "type": fn.result_type, "kind": kind}


def _bool_args(fn: OracleFn) -> list[tuple]:
    vals = (True, False)
    others = (True, False, 0, 1, -1, 2, 2 ** 63, -(2 ** 200))
    out = []
    if fn.arity == 2:
        for b in vals:
            for o in others:
                for t in ((b, o), (o, b)):
                    if t not in out and fn.domain(*t):
                        out.append(t)
    return out


def cases(name: str, per_fn: int) -> list[dict]:
    """Every verification sends, per function:
      - the exhaustive small grid and the boundary pairs, positionally;
      - the same set again with every argument passed BY KEYWORD, and the small grid again in MIXED form
        (first positional, rest keyword);
      - every equal-argument case again with the arguments ALIASED (one shared object, as `f(x, x)`), next to the
        fresh-object copy (C2-1: identity must not change the answer);
      - bool arguments where the function's domain includes them (`accepts_bool`);
      - `per_fn` fresh random draws (mixed forms) plus `max(4, per_fn // 4)` random EQUAL pairs, fresh and aliased.
    Interleaved and shuffled, with the oracle's expected value attached (kept in the parent; only
    id/call/args/names/alias are sent to the child)."""
    out = []
    fns = ORACLES[name]
    for fn in fns:
        fixed = _fixed_args(fn)
        grid = set(itertools.product(fn.grid, repeat=fn.arity))
        for args in fixed:
            out.append(_case(fn, args, "fixed"))
            out.append(_case(fn, args, "fixed-kw", "kw"))
            if args in grid and fn.arity >= 2:
                out.append(_case(fn, args, "fixed-mixed", "mixed"))
            if fn.arity >= 2 and len(set(args)) == 1:
                out.append(_case(fn, args, "fixed-alias", "pos", alias=True))
                out.append(_case(fn, args, "fixed-alias-kw", "kw", alias=True))
        if fn.accepts_bool:
            for args in _bool_args(fn):
                out.append(_case(fn, args, "bool"))
                out.append(_case(fn, args, "bool-kw", "kw"))
    forms = ("pos", "kw", "mixed")
    for _ in range(per_fn):
        for fn in fns:
            while True:
                args = [fn.gen() for _ in range(fn.arity)]
                if fn.domain(*args):
                    break
            out.append(_case(fn, args, "random", _rng.choice(forms) if fn.arity >= 2 else "pos"))
    for fn in fns:
        if fn.arity < 2:
            continue
        for _ in range(max(4, per_fn // 4)):
            while True:
                x = fn.gen()
                args = [x] * fn.arity
                if fn.domain(*args):
                    break
            out.append(_case(fn, args, "random-equal"))
            out.append(_case(fn, args, "random-alias", _rng.choice(forms), alias=True))
    _rng.shuffle(out)
    for i, c in enumerate(out):
        c["id"] = i
    return out


def wire(case: dict) -> dict:
    """The only part of a case that is sent to the untrusted child."""
    return {"id": case["id"], "call": case["call"], "args": case["args"], "names": case["names"],
            "alias": case["alias"]}


def check_value(case: dict, value: Any) -> bool:
    want_t = case["type"]
    if type(value) is not want_t:  # exact type of the JSON-decoded value; bool/float never pass for int
        return False
    return value == case["expect"]  # both sides are plain values built by the parent's own json decoder / oracle
