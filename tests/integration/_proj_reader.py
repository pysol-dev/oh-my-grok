"""Poll a boulder file; every read must parse and the generation must never decrease."""
import json
import os
import sys
import time

path, stop = sys.argv[1], sys.argv[2]
last, reads = -1, 0
while not os.path.exists(stop):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        time.sleep(0.001)
        continue
    try:
        g = json.loads(raw)["x_omo_grok"]["generation"]
    except Exception as e:  # noqa: BLE001
        print(f"UNPARSEABLE after {reads} reads: {e!r}: {raw[:80]!r}")
        sys.exit(1)
    if g < last:
        print(f"REGRESSED {last} -> {g}")
        sys.exit(1)
    last, reads = g, reads + 1
print(f"OK reads={reads} last={last}")
