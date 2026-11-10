"""Pure diagnostics arithmetic (spec/ADAPTIVE.md 7.5): keys that survive line drift, multiset delta, suppressions."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from fnmatch import fnmatch

SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}
_POS = re.compile(r"(:\d+){1,2}\b|\bline \d+\b|\bcolumn \d+\b|\bat \d+:\d+\b", re.IGNORECASE)
_WS = re.compile(r"\s+")


def norm_code(code) -> str:
    if isinstance(code, dict):
        code = code.get("value")
    return "" if code is None else str(code)


def norm_message(msg) -> str:
    return _WS.sub(" ", _POS.sub("#", str(msg or ""))).strip()


def normalize(path: str, d: dict) -> dict:
    sev = d.get("severity")
    rng = (d.get("range") or {}).get("start") or {}
    return {"path": path, "severity": SEVERITY.get(sev, "error" if sev is None else str(sev)),
            "code": norm_code(d.get("code")), "message": norm_message(d.get("message")),
            "line": rng.get("line"), "character": rng.get("character"), "source": d.get("source")}


def key(d: dict) -> tuple:
    return (d["path"], d["severity"], d["code"], d["message"])


def new_diagnostics(base: list[dict], cand: list[dict], fail_on=("error",)) -> list[dict]:
    """Candidate diagnostics in excess of the base's, per key (multiset difference), restricted to ``fail_on``."""
    have = Counter(key(d) for d in base)
    out = []
    for d in cand:
        k = key(d)
        if have[k] > 0:
            have[k] -= 1
        elif d["severity"] in fail_on:
            out.append(d)
    return out


def match_any(path: str, patterns) -> bool:
    return any(fnmatch(path, p) or path.startswith(p.rstrip("*")) and p.endswith("/") for p in patterns)


def suppressions(diff_text: str, patterns: list[str]) -> list[dict]:
    """Net new suppression comments per (path, pattern) in a unified diff: added occurrences minus removed ones."""
    compiled = [(p, re.compile(p)) for p in patterns]
    net: dict[tuple, int] = defaultdict(int)
    path = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            tgt = line[4:].strip()
            path = None if tgt == "/dev/null" else tgt[2:] if tgt.startswith("b/") else tgt
            continue
        if line.startswith("--- "):
            continue
        if path is None or not line or line[0] not in "+-":
            continue
        sign = 1 if line[0] == "+" else -1
        for name, rx in compiled:
            n = len(rx.findall(line[1:]))
            if n:
                net[(path, name)] += sign * n
    return [{"path": p, "pattern": n, "net_added": v} for (p, n), v in sorted(net.items()) if v > 0]


def config_violations(changed: list[str], config_files: list[str]) -> list[str]:
    names = set(config_files)
    return sorted(p for p in changed if p.rsplit("/", 1)[-1] in names)
