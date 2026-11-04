"""UNTRUSTED oracle probe child: source text builder (the parent never imports candidate code).

The child is run inside the sandbox as `<python> [flags] -c <bootstrap>` so that the candidate is imported exactly
the way `python -c 'import calc'` would import it: the FIRST statement of the program is `import <module>`
(sys.path[0] is the project directory, __main__ holds only its standard dunders, no script path in argv, no
harness file on disk). Only after the import does the bootstrap set up its protocol channel. It holds no secret,
nonce, result path or result writer: everything it says is untrusted data to the parent.

Protocol (line-delimited JSON on stdin/stdout):
  child -> parent  {"ready": true}                               after importing the candidate modules
  parent -> child  {"id": n, "call": "mod.fn", "args": [...],   ONE request at a time; the next input is only
                   "names": ["", "b"], "alias": [[0, 1]]}        (names: "" = positional, else keyword;
                                                                 alias [i, j]: argument j is the same object as i)
  child -> parent  {"id": n, "value": <raw result>}              generated/sent after this answer arrives
                   {"id": n, "error": "..."}
  parent -> child  {"end": true}
Every candidate call is made from the program's TOP LEVEL (the module frame of `-c`), exactly like a script calling
`calc.add(...)`: the protocol loop is a generator that hands each call out to a top-level loop. With padding 0 the
call runs at the stock recursion limit with the same frame depth as real use. With random padding n > 0 the call
goes through n extra frames, and for that call only the limit is raised by exactly the number of frames counted
below the callee (measured with the frame chain, not estimated), then restored; the callee never gets more headroom
than a top-level call in a normal process.
The bootstrap text deliberately contains no harness marker words (checked by tests)."""
from __future__ import annotations

import re

_MOD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

_BODY = '''
def _main(names, pad_max, hard_exit):
    import json, os, random, sys
    chan = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    mods = {{n: sys.modules[n] for n in names}}
    rnd = random.SystemRandom()
    stock = sys.getrecursionlimit()

    def frames():
        f, n = sys._getframe(1), 0
        while f is not None:
            n, f = n + 1, f.f_back
        return n

    def deep(n, f, a, k):
        if n > 0:
            return deep(n - 1, f, a, k)
        # f must get exactly the headroom it has when called from a script's top level (where it is at depth 2):
        # raise the limit by the frames measured below it, for this one call only
        sys.setrecursionlimit(stock + frames() - 1)
        try:
            return f(*a, **k)
        finally:
            sys.setrecursionlimit(stock)
    chan.write(json.dumps({{"ready": True}}) + "\\n")
    res = None
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("end"):
            break
        rid = req["id"]
        try:
            m, fn = req["call"].rsplit(".", 1)
            vals = list(req["args"])
            for i, j in req.get("alias") or ():
                vals[j] = vals[i]
            names = req.get("names") or [""] * len(vals)
            pos = [x for x, nm in zip(vals, names) if not nm]
            kw = {{nm: x for x, nm in zip(vals, names) if nm}}
            f = getattr(mods[m], fn)
            n = rnd.randrange(pad_max + 1)
            # the call itself happens at the program's top level (see below), unpadded at the stock limit
            ok, v = yield ((f, pos, kw) if n == 0 else (deep, (n, f, pos, kw), {{}}))
            msg = json.dumps({{"id": rid, "value": v}}) if ok else json.dumps({{"id": rid, "error": v}})
        except BaseException as e:
            msg = json.dumps({{"id": rid, "error": type(e).__name__}})
        chan.write(msg + "\\n")
    chan.flush()
    if hard_exit:
        os._exit(0)


_c = _main({names!r}, {pad}, {hard})
_t = next(_c, None)
while _t is not None:
    _f, _a, _k = _t
    try:
        _v = (True, _f(*_a, **_k))
    except BaseException as _e:
        _v = (False, type(_e).__name__)
    try:
        _t = _c.send(_v)
    except StopIteration:
        _t = None
'''


def bootstrap(modules: list[str], pad_max: int = 0, hard_exit: bool = False, add_cwd: bool = False) -> str:
    if not modules or not all(isinstance(m, str) and _MOD.match(m) for m in modules):
        raise ValueError("bad module names")
    head = "__import__('sys').path.insert(0, '')\n" if add_cwd else ""
    imports = "".join(f"import {m}\n" for m in modules)
    return head + imports + _BODY.format(names=list(modules), pad=int(pad_max), hard=bool(hard_exit))
