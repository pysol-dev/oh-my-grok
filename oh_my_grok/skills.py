"""Skill packs delivered to bots in task packets. Delivery is recorded as an engine_delivered receipt, which proves
what text was put in a packet, never that a model read or followed it (spec/ADAPTIVE.md section 9)."""
from __future__ import annotations

import hashlib

from .errors import ValidationError
from .paths import CODE_ROOT

SKILLS_DIR = CODE_ROOT / "skills"
PACKS_BY_KIND = {"implement": ["git-master"]}


def load(name: str) -> dict:
    if not name.replace("-", "").isalnum():
        raise ValidationError(f"invalid skill name {name!r}")
    p = SKILLS_DIR / name / "SKILL.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ValidationError(f"skill {name!r} is not installed: {p}") from e
    return {"name": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}


def pack_for(kind: str) -> list[dict]:
    return [load(n) for n in PACKS_BY_KIND.get(kind, [])]


def pack_hash(pack: list[dict]) -> str:
    return hashlib.sha256("\n".join(f"{s['name']}:{s['sha256']}" for s in pack).encode()).hexdigest()
