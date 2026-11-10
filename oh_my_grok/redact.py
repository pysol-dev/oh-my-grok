"""Redaction of secrets from any text leaving the process (logs, status, export, errors)."""
from __future__ import annotations

import json
import re
from typing import Any

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(\"?)[^\"\r\n]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|webhook[_-]?key)(\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(xai|sk|ghp|gho|github_pat)[-_][A-Za-z0-9_\-]{16,}\b"), "[REDACTED TOKEN]"),
]

# Keys whose values are always secret regardless of content.
SECRET_KEYS = re.compile(r"(?i)^(authorization|key|bearer|token|secret|password|webhook_key|url_secret|hook_url|"
                         r"webhook_url|lease_token|claim_token|webhook)$")

_known: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _known.add(value)


def redact(text: Any) -> str:
    if not isinstance(text, str):
        text = str(text)
    for s in sorted(_known, key=len, reverse=True):
        text = text.replace(s, "[REDACTED]")
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


_URL = re.compile(r"(?i)\b(https?://)(?:[^/\s\"'@]*@)?([A-Za-z0-9.\-\[\]:]+)[^\s\"'<>]*")


def redact_urls(text: Any) -> str:
    """Secret redaction plus: every http(s) URL keeps only scheme://host. Used for dispatch errors, which may echo a
    webhook URL even in a process that never loaded the hooks file (so no secret was registered)."""
    return _URL.sub(r"\1\2/[REDACTED-URL]", redact(text))


def redact_obj(obj: Any) -> Any:
    """Redact a JSON-able object by round-tripping each string."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and SECRET_KEYS.search(k):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def safe_json(obj: Any, **kw) -> str:
    return json.dumps(redact_obj(obj), **kw)
