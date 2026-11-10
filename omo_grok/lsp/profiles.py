"""LSP profile configuration (config/lsp.json): validation, defaults, language detection. Fail-closed by design:
a code file whose extension has no enabled profile is reported, never ignored."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import expand_placeholders
from ..errors import ValidationError
from ..paths import CODE_ROOT

PROFILE_DEFAULTS = {
    "enabled": True, "mode": "auto", "config_files": [], "settings": {}, "init_options": {}, "env": {},
    "path_dirs": [], "tool_dirs": [], "fail_on": ["error"], "quiet_seconds": 0.5, "file_timeout_seconds": 30,
    "startup_timeout_seconds": 60, "address_space_mb": 4096, "exclude": [], "suppression_patterns": [],
    "language_ids": {}, "max_file_bytes": 1_000_000, "max_files": 400,
}
REQUIRED = ("language", "extensions", "argv", "canary")
KNOWN = set(PROFILE_DEFAULTS) | set(REQUIRED)


def config_path(config_dir: Path) -> Path:
    p = Path(config_dir) / "lsp.json"
    return p if p.exists() else CODE_ROOT / "config" / "lsp.json"


def load(config_dir: Path) -> dict:
    p = config_path(config_dir)
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise ValidationError(f"cannot read LSP config {p}: {e}") from e
    return validate(expand_placeholders(raw))


def validate(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValidationError("lsp.json must be an object with version 1")
    codes = raw.get("code_extensions")
    if not isinstance(codes, dict) or not codes or not all(
            isinstance(k, str) and k.startswith(".") and isinstance(v, str) for k, v in codes.items()):
        raise ValidationError("code_extensions must map '.ext' to a language name")
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValidationError("profiles must be a non-empty object")
    out = {}
    for name, prof in profiles.items():
        if not isinstance(prof, dict):
            raise ValidationError(f"profile {name!r} must be an object")
        unknown = set(prof) - KNOWN
        if unknown:
            raise ValidationError(f"profile {name!r} has unknown keys", unknown=sorted(unknown))
        missing = [k for k in REQUIRED if k not in prof]
        if missing:
            raise ValidationError(f"profile {name!r} is missing {missing}")
        p = {**PROFILE_DEFAULTS, **prof}
        if not (isinstance(p["argv"], list) and p["argv"] and all(isinstance(a, str) for a in p["argv"])
                and p["argv"][0].startswith("/")):
            raise ValidationError(f"profile {name!r}: argv must be a list whose first element is an absolute path")
        if not (isinstance(p["extensions"], list) and p["extensions"] and all(
                isinstance(e, str) and e.startswith(".") for e in p["extensions"])):
            raise ValidationError(f"profile {name!r}: extensions must be a non-empty list of '.ext'")
        if p["mode"] not in ("auto", "pull", "push"):
            raise ValidationError(f"profile {name!r}: mode must be auto|pull|push")
        if not set(p["fail_on"]) <= {"error", "warning", "information", "hint"} or not p["fail_on"]:
            raise ValidationError(f"profile {name!r}: fail_on must be severities")
        cn = p["canary"]
        if not (isinstance(cn, dict) and all(k in cn for k in ("bad", "good"))
                and all(isinstance(cn[k].get("path"), str) and isinstance(cn[k].get("text"), str) for k in ("bad", "good"))
                and cn["bad"].get("expect_codes")):
            raise ValidationError(f"profile {name!r}: canary needs bad{{path,text,expect_codes}} and good{{path,text}}")
        for k in ("bad", "good"):
            if "/" in cn[k]["path"] or cn[k]["path"].startswith("."):
                raise ValidationError(f"profile {name!r}: canary paths are plain, non-hidden file names")
        out[name] = p
    return {"code_extensions": codes, "profiles": out, "limits": raw.get("limits", {})}


def enabled_profiles(cfg: dict) -> dict[str, dict]:
    return {n: p for n, p in cfg["profiles"].items() if p["enabled"]}


def ext_of(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[1].lower() if "." in base.lstrip(".") else ""


def code_files(cfg: dict, paths: list[str]) -> dict[str, list[str]]:
    """Group changed paths that look like code by language name."""
    out: dict[str, list[str]] = {}
    for p in paths:
        lang = cfg["code_extensions"].get(ext_of(p))
        if lang:
            out.setdefault(lang, []).append(p)
    return out


def profiles_for(cfg: dict, paths: list[str]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """(enabled profiles that apply to at least one path, languages with code paths but no enabled profile)."""
    en = enabled_profiles(cfg)
    exts = {ext_of(p) for p in paths}
    apply = {n: p for n, p in en.items() if exts & {e.lower() for e in p["extensions"]}}
    uncovered: dict[str, list[str]] = {}
    covered_exts = {e.lower() for p in en.values() for e in p["extensions"]}
    for p in paths:
        e = ext_of(p)
        lang = cfg["code_extensions"].get(e)
        if lang and e not in covered_exts:
            uncovered.setdefault(lang, []).append(p)
    return apply, uncovered
