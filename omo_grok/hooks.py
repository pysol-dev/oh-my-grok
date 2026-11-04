"""Webhook secret store: state/secrets/hooks.json, mode 0600 inside a 0700 dir.

This prevents *accidental disclosure* only; any same-account Bot with shell access can read it."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

from .errors import IntegrityError_, ValidationError
from .paths import Paths
from .redact import register_secret
from .states import ROLES


def _check_perms(paths: Paths) -> None:
    d = paths.secrets_dir
    if d.exists() and stat.S_IMODE(d.stat().st_mode) & 0o077:
        raise IntegrityError_(f"secrets dir permissions too open: {oct(stat.S_IMODE(d.stat().st_mode))}")
    f = paths.hooks_path
    if f.exists():
        if f.is_symlink():
            raise IntegrityError_("hooks.json must not be a symlink")
        if stat.S_IMODE(f.stat().st_mode) & 0o077:
            raise IntegrityError_(f"hooks.json permissions too open: {oct(stat.S_IMODE(f.stat().st_mode))}")


def load_hooks(paths: Paths) -> dict:
    _check_perms(paths)
    if not paths.hooks_path.exists():
        return {}
    data = json.loads(paths.hooks_path.read_text())
    for v in data.values():
        register_secret(v.get("key"))
        register_secret(v.get("url"))
    return data


_PASTE_MARKERS = ("\x1b[200~", "\x1b[201~", "[200~", "[201~")
_PAIRS = (('"', '"'), ("'", "'"), ("<", ">"), ("`", "`"))


def sanitize_input(raw: str) -> tuple[str, bool]:
    """Clean a pasted URL or key: drop bracketed-paste markers (with or without ESC), every ASCII control char
    (C0 + DEL, incl. CR/LF/TAB/ESC) and other non-printable chars, surrounding whitespace, and ONE matching pair of
    surrounding quotes or angle brackets. Returns (clean, removed_nonprintable). Never logs the value."""
    s = raw or ""
    for m in _PASTE_MARKERS:
        s = s.replace(m, "")
    kept = "".join(c for c in s if c.isprintable() or c == " ")
    removed = kept != s
    kept = kept.strip()
    for lo, hi in _PAIRS:
        if len(kept) >= 2 and kept[0] == lo and kept[-1] == hi:
            kept = kept[1:-1].strip()
            break
    return kept, removed or (raw or "") != s


def url_diagnostic(clean: str, removed_nonprintable: bool) -> str:
    """A diagnostic that NEVER contains the value: length, emptiness, scheme class, host presence, cleanup flag."""
    try:
        u = urlparse(clean)
        scheme, has_host = u.scheme.lower(), bool(u.hostname)
    except ValueError:
        scheme, has_host = "unparseable", False
    if scheme not in ("http", "https", "", "unparseable"):
        scheme = "other"
    return (f"length={len(clean)} empty={not clean} scheme={scheme or 'empty'} has_host={has_host} "
            f"non_printable_or_paste_markers_removed={removed_nonprintable}")


def validate_url(url: str) -> None:
    try:
        u = urlparse(url)
        host = u.hostname
        u.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise ValidationError("webhook URL is malformed (unparseable host or port; value not shown)") from None
    insecure_ok = os.environ.get("OMO_ALLOW_INSECURE_HOOKS") == "1" and host in ("127.0.0.1", "localhost")
    if u.scheme != "https" and not (u.scheme == "http" and insecure_ok):
        raise ValidationError("webhook URL must be https")
    if not host:
        raise ValidationError("webhook URL has no host")


def validate_key(key: str) -> str | None:
    """Return a problem description (never containing the key) or None."""
    if len(key) < 8 or any(c.isspace() for c in key):
        return f"webhook key looks invalid (too short or contains whitespace; length={len(key)})"
    return None


def set_hook(paths: Paths, role: str, url: str, key: str) -> None:
    if role not in ROLES:
        raise ValidationError(f"unknown role {role!r}")
    url, key = sanitize_input(url)[0], sanitize_input(key)[0]
    validate_url(url)
    key_problem = validate_key(key)
    if key_problem:
        raise ValidationError(key_problem)
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.secrets_dir, 0o700)
    data = load_hooks(paths)
    data[role] = {"url": url, "key": key, "set_at": datetime.now(timezone.utc).isoformat()}
    fd, tmp = tempfile.mkstemp(dir=paths.secrets_dir, prefix=".hooks.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, paths.hooks_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    register_secret(key)
    register_secret(url)


def hook_status(paths: Paths) -> dict[str, bool]:
    try:
        data = load_hooks(paths)
    except IntegrityError_:
        data = {}
    return {r: bool(data.get(r, {}).get("url") and data.get(r, {}).get("key")) for r in ROLES}
