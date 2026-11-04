"""UNTRUSTED oracle probe child: source text builder (the parent never imports candidate code).

The child is run inside the sandbox as `<python> [flags] -c <bootstrap>` so that the candidate is imported exactly
the way `python -c 'import calc'` would import it: the FIRST statement of the program is `import <module>`
(sys.path[0] is the project directory, __main__ holds only its standard dunders, no script path in argv, no
harness file on disk). Only after the import does the bootstrap set up its protocol channel. It holds no secret,
nonce, result path or result writer: everything it says is untrusted data to the parent.

Protocol (line-delimited JSON on stdin/stdout):
  child -> parent  {"ready": true}                               after importing the candidate modules
  parent -> child  {"id": n, "call": "mod.fn", "args": [...]}    ONE request at a time; the next input is only
  child -> parent  {"id": n, "value": <raw result>}              generated/sent after this answer arrives
                   {"id": n, "error": "..."}
  parent -> child  {"end": true}
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

    def deep(n, f, a):
        return f(*a) if n <= 0 else deep(n - 1, f, a)
    chan.write(json.dumps({{"ready": True}}) + "\\n")
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("end"):
            break
        rid = req["id"]
        try:
            m, fn = req["call"].rsplit(".", 1)
            v = deep(rnd.randrange(pad_max + 1), getattr(mods[m], fn), req["args"])
            msg = json.dumps({{"id": rid, "value": v}})
        except BaseException as e:
            msg = json.dumps({{"id": rid, "error": type(e).__name__}})
        chan.write(msg + "\\n")
    chan.flush()
    if hard_exit:
        os._exit(0)


_main({names!r}, {pad}, {hard})
'''


def bootstrap(modules: list[str], pad_max: int = 0, hard_exit: bool = False, add_cwd: bool = False) -> str:
    if not modules or not all(isinstance(m, str) and _MOD.match(m) for m in modules):
        raise ValueError("bad module names")
    head = "__import__('sys').path.insert(0, '')\n" if add_cwd else ""
    imports = "".join(f"import {m}\n" for m in modules)
    return head + imports + _BODY.format(names=list(modules), pad=int(pad_max), hard=bool(hard_exit))
