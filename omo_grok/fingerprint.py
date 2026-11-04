"""Code+config fingerprint, so a long-running process (the MCP server) can detect that what is on disk changed
since it started and refuse to serve with stale in-memory code (e.g. an old verifier against a new child script)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .paths import CODE_ROOT, get_paths


def _files() -> list[Path]:
    out = sorted((CODE_ROOT / "omo_grok").rglob("*.py"))
    cfg = get_paths().config_dir
    if cfg.is_dir():
        out += sorted(p for p in cfg.iterdir() if p.is_file() and p.suffix in (".json", ".txt"))
    return out


def fingerprint() -> str:
    h = hashlib.sha256()
    for f in _files():
        h.update(str(f).encode() + b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()
