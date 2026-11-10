"""Subprocess helper for projection concurrency/crash tests. Talks to the engine only through its public API."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from omo_grok.engine import Engine  # noqa: E402


def main():
    mode, job = sys.argv[1], sys.argv[2]
    e = Engine()
    try:
        if mode == "checkpoint":
            task, attempt, n = sys.argv[3], sys.argv[4], int(sys.argv[5])
            for i in range(n):
                e.checkpoint(job, task, attempt, f"progress {i} from pid")
        elif mode == "claim":
            task, dispatch = sys.argv[3], sys.argv[4]
            pkt = e.claim(job, task, dispatch)
            print(json.dumps({"attempt_id": pkt["attempt_id"]}))
        elif mode == "status":
            print(json.dumps(e.status(job)["phase"]))
    finally:
        e.close()


if __name__ == "__main__":
    main()
