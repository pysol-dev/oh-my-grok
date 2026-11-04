"""UNTRUSTED oracle probe child. Spawned by verifier.py as `python -I -B -X pycache_prefix=<tmp> oracle_child.py
<module> [<module> ...]` with cwd = the candidate checkout and a scrubbed environment. It holds no secret, nonce,
result path or result writer: everything it says is treated as untrusted data by the parent.

Protocol (line-delimited JSON):
  child -> parent  {"ready": true}                               after importing the candidate modules
  parent -> child  {"id": n, "call": "mod.fn", "args": [...]}    ONE request at a time; the next input is only
  child -> parent  {"id": n, "value": <raw result>}              generated/sent after this answer arrives
                   {"id": n, "error": "..."}
  parent -> child  {"end": true}
The parent decides pass/fail by recomputing every expected value with its own oracle."""
import json
import os
import sys


def main():
    sys.path.insert(0, os.getcwd())
    sys.dont_write_bytecode = True
    out = os.fdopen(os.dup(1), "w", buffering=1)  # protocol channel; candidate prints go to stderr
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    mods = {}
    for name in sys.argv[1:]:
        mods[name] = __import__(name, fromlist=["_"])
    out.write(json.dumps({"ready": True}) + "\n")
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("end"):
            break
        rid = req["id"]
        try:
            modname, fn = req["call"].rsplit(".", 1)
            value = getattr(mods[modname], fn)(*req["args"])
            msg = json.dumps({"id": rid, "value": value})
        except BaseException as e:  # noqa: BLE001 - reported as a failed probe
            msg = json.dumps({"id": rid, "error": f"{type(e).__name__}"})
        out.write(msg + "\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
