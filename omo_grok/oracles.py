"""Trusted oracles: expected behaviour defined in the orchestrator's own code, never in the candidate tree.

The verifier parent (which never imports candidate code) builds the cases for every verification from three parts:
  1. an EXHAUSTIVE small grid (every argument tuple in [-16, 16]^arity, filtered by the function's domain),
  2. fixed BOUNDARY values (0, +-1, +-2, sign mixes, 2**31/2**32/2**53/2**63/2**64 neighbourhoods, +-10**18,
     +-2**200, +-2**1000) combined pairwise,
  3. FRESH random draws from the OS CSPRNG across magnitude bands.
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


def _boundary_ints() -> list[int]:
    """Zero, +-1, +-2, the edges of the small grid, and both sides of the common machine-int / float-exact limits
    (2**31, 2**32, 2**53, 2**63, 2**64), plus 10**18 and arbitrary-precision magnitudes, with both signs."""
    pos = {1, 2, 16, 17, 2 ** 31 - 1, 2 ** 31, 2 ** 32, 2 ** 53 + 1, 2 ** 63 - 1, 2 ** 63, 2 ** 64, 10 ** 18,
           2 ** 200, 2 ** 1000}
    return sorted({0} | pos | {-x for x in pos})


BOUNDARY_INTS = _boundary_ints()


def rand_int() -> int:
    """Mix of magnitudes: small, medium, 64-bit-ish and arbitrary precision, both signs."""
    band = _rng.randrange(5)
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


ORACLES: dict[str, list[OracleFn]] = {
    "calc-arith": [
        OracleFn("calc.add", 2, rand_int, lambda a, b: a + b, int),
        OracleFn("calc.mul", 2, rand_int, lambda a, b: a * b, int),
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


def cases(name: str, per_fn: int) -> list[dict]:
    """Exhaustive small grid + boundary cases + `per_fn` fresh random cases per function, interleaved and shuffled,
    with the oracle's expected value attached (kept in the parent; only id/call/args are ever sent to the child)."""
    out = []
    fns = ORACLES[name]
    for fn in fns:
        for args in _fixed_args(fn):
            out.append({"call": fn.call, "args": list(args), "expect": fn.expect(*args), "type": fn.result_type,
                        "kind": "fixed"})
    for _ in range(per_fn):
        for fn in fns:
            while True:
                args = [fn.gen() for _ in range(fn.arity)]
                if fn.domain(*args):
                    break
            out.append({"call": fn.call, "args": args, "expect": fn.expect(*args), "type": fn.result_type,
                        "kind": "random"})
    _rng.shuffle(out)
    for i, c in enumerate(out):
        c["id"] = i
    return out


def check_value(case: dict, value: Any) -> bool:
    want_t = case["type"]
    if type(value) is not want_t:  # exact type of the JSON-decoded value; bool/float never pass for int
        return False
    return value == case["expect"]  # both sides are plain values built by the parent's own json decoder / oracle
