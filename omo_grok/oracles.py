"""Trusted oracles: expected behaviour defined in the orchestrator's own code, never in the candidate tree.

The verifier parent (which never imports candidate code) draws FRESH random inputs per run from the OS CSPRNG,
sends them one at a time to an untrusted child that calls the candidate, and compares the raw JSON values it gets
back with what the oracle computes here. A candidate cannot pass without returning the right value for inputs it
could not know in advance."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Callable

_rng = secrets.SystemRandom()


def rand_int() -> int:
    """Mix of magnitudes: small, 64-bit-ish and arbitrary precision, both signs."""
    band = _rng.randrange(4)
    if band == 0:
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


ORACLES: dict[str, list[OracleFn]] = {
    "calc-arith": [
        OracleFn("calc.add", 2, rand_int, lambda a, b: a + b, int),
        OracleFn("calc.mul", 2, rand_int, lambda a, b: a * b, int),
    ],
}


def cases(name: str, per_fn: int) -> list[dict]:
    """Fresh random cases, interleaved across functions, with the oracle's expected value attached (kept in the
    parent; only id/call/args are ever sent to the child)."""
    out = []
    fns = ORACLES[name]
    for i in range(per_fn):
        for fn in fns:
            args = [fn.gen() for _ in range(fn.arity)]
            out.append({"call": fn.call, "args": args, "expect": fn.expect(*args), "type": fn.result_type})
    _rng.shuffle(out)
    for i, c in enumerate(out):
        c["id"] = i
    return out


def check_value(case: dict, value: Any) -> bool:
    want_t = case["type"]
    if type(value) is not want_t:  # exact type of the JSON-decoded value; bool/float never pass for int
        return False
    return value == case["expect"]  # both sides are plain values built by the parent's own json decoder / oracle
