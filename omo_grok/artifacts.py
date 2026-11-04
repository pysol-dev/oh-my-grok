"""Artifact storage: SHA-256 on ingest, validation on read, path-traversal and symlink defence."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath

from .errors import IntegrityError_, ValidationError

SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_INLINE_BYTES = 1_000_000
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(p: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def safe_rel_path(rel: str) -> PurePosixPath:
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        raise ValidationError("artifact path must be a non-empty POSIX relative path")
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise IntegrityError_("absolute artifact path rejected", path=rel)
    for part in rel.split("/"):  # raw split: reject '.', '..', '' (//) before any normalisation
        if part in ("..", ".") or not SAFE_COMPONENT.match(part):
            raise IntegrityError_("unsafe artifact path component rejected", path=rel)
    if len(p.parts) > 8:
        raise ValidationError("artifact path too deep")
    return p


def resolve_inside(root: Path, rel: str) -> Path:
    """Map rel onto root, refusing any symlink on the way and any escape from root."""
    p = safe_rel_path(rel)
    root = root.resolve()
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise IntegrityError_("symlink in artifact path rejected", path=rel)
    final = cur.resolve()
    if root != final and root not in final.parents:
        raise IntegrityError_("artifact path escapes job artifact root", path=rel)
    return cur


def write_inline(root: Path, rel: str, content: str | bytes) -> tuple[str, int]:
    data = content.encode("utf-8") if isinstance(content, str) else content
    if len(data) > MAX_INLINE_BYTES:
        raise ValidationError(f"inline artifact exceeds {MAX_INLINE_BYTES} bytes")
    root.mkdir(parents=True, exist_ok=True)
    target = resolve_inside(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    # re-check after mkdir (no symlink introduced in between for our own dirs)
    resolve_inside(root, rel)
    digest = hashlib.sha256(data).hexdigest()
    if target.exists():
        existing, _ = sha256_file(target)
        if existing != digest:
            raise IntegrityError_("artifact already exists with different content; artifacts are immutable", path=rel)
        return digest, len(data)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return digest, len(data)


def verify_file(root: Path, rel: str, expected_sha: str) -> int:
    if not SHA_RE.match(expected_sha or ""):
        raise ValidationError("sha256 must be 64 lowercase hex chars")
    target = resolve_inside(root, rel)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        raise IntegrityError_("artifact missing", path=rel) from None
    if not stat.S_ISREG(st.st_mode):
        raise IntegrityError_("artifact is not a regular file", path=rel)
    digest, size = sha256_file(target)
    if digest != expected_sha:
        raise IntegrityError_("artifact hash mismatch (corrupt or replaced)", path=rel, expected=expected_sha, actual=digest)
    return size


def read_text(root: Path, rel: str, expected_sha: str, limit: int = MAX_INLINE_BYTES) -> str:
    verify_file(root, rel, expected_sha)
    with open(resolve_inside(root, rel), "rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


def canonical_plan_bytes(text: str) -> bytes:
    """Canonical plan form: UTF-8, LF newlines, no trailing whitespace per line, single final newline."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return ("\n".join(lines) + "\n").encode("utf-8")
