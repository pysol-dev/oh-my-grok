"""Separate-process actor for concurrency/restart tests. Uses only the public Engine API.
argv: op job task dispatch clock go_file [payload-kind]"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_grok.engine import Engine  # noqa: E402
from omo_grok.errors import OmoError  # noqa: E402
from omo_grok.transport import HttpResult  # noqa: E402

RESULTS = {
    "explore": ({"repo_map": "calc", "files": [], "tests": [], "constraints": [], "questions": []},
                [{"kind": "explore", "name": "explore.md", "content": "EXPLORER (subprocess)"}]),
    "research": ({"sources": ["https://docs.python.org/3/"], "api_requirements": [], "risks": []},
                 [{"kind": "research", "name": "research.md", "content": "LIBRARIAN (subprocess)"}]),
}


def main():
    op, job, task, disp, clock, go = sys.argv[1:7]
    e = Engine(transport=lambda *a: HttpResult(200, None, None), clock=lambda: float(clock))
    e.policy["inline_dispatch"] = False
    deadline = time.time() + 20
    while go != "-" and not os.path.exists(go):  # start barrier
        if time.time() > deadline:
            sys.exit(3)
        time.sleep(0.002)
    out = {"pid": os.getpid()}
    try:
        pkt = e.claim(job, task, disp)
        out["claimed"] = pkt["attempt_id"]
        print(json.dumps(out), flush=True)
        if op == "claim_and_hang":
            time.sleep(600)
        if op == "claim_complete":
            res, refs = RESULTS[sys.argv[7]]
            r = e.complete(job, task, pkt["attempt_id"], res, refs)
            out["completed"] = r["task_status"]
    except OmoError as ex:
        out["error"] = type(ex).__name__
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
