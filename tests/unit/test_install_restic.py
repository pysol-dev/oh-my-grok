"""Pin and installer for the restic binary. The cache file is never chmod'd."""
import bz2
import hashlib
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "scripts" / "install-restic.sh"
LOCK = ROOT / "tooling" / "restic.lock"


def _fields(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key] = value
    return out


def test_lock_pins_restic_0_19_1_and_records_the_signer():
    fields = _fields(LOCK.read_text())
    assert fields["version"] == "0.19.1"
    assert fields["archive"] == "restic_0.19.1_linux_amd64.bz2"
    assert fields["sha256"] == "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c"
    assert fields["binary_sha256"] == "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639"
    assert fields["gpg_signer"] == "CF8F18F2844575973F79D4E191A6868BD3F7A907"
    assert "CF8F 18F2 8445 7597 3F79 D4E1 91A6 868B D3F7 A907" in LOCK.read_text()


def _script_bytes() -> bytes:
    return b"#!/bin/sh\necho 'restic 0.19.1 compiled with go'\n"


def _run(lock: Path, tools: Path, cache: Path):
    env = os.environ.copy()
    env["OMG_RESTIC_LOCK"] = str(lock)
    env["OMG_TOOLS"] = str(tools)
    env["OMG_RESTIC_CACHE"] = str(cache)
    env.pop("OMG_RESTIC_CACHE_DIR", None)
    return subprocess.run([str(INSTALL)], capture_output=True, text=True, env=env, timeout=30, check=False)


def test_cache_fallback_verifies_sha_then_chmods_only_the_tools_copy(tmp_path):
    raw = _script_bytes()
    archive = bz2.compress(raw)
    lock = tmp_path / "restic.lock"
    lock.write_text(
        "version=0.19.1\n"
        "archive=restic_0.19.1_linux_amd64.bz2\n"
        "url=\n"
        f"sha256={hashlib.sha256(archive).hexdigest()}\n"
        f"binary_sha256={hashlib.sha256(raw).hexdigest()}\n"
        "cache_name=restic_0.19.1_linux_amd64\n"
        "gpg_signer=CF8F18F2844575973F79D4E191A6868BD3F7A907\n"
    )
    cache = tmp_path / "store" / "restic_0.19.1_linux_amd64.bz2"
    cache.parent.mkdir()
    cache.write_bytes(archive)
    os.chmod(cache, 0o666)
    before = stat.S_IMODE(cache.stat().st_mode)
    tools = tmp_path / "tools"
    result = _run(lock, tools, cache)
    assert result.returncode == 0, result.stderr
    dest = tools / "restic"
    assert dest.read_bytes() == raw
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert stat.S_IMODE(cache.stat().st_mode) == before == 0o666


def test_cache_sha_mismatch_leaves_the_cache_mode_alone(tmp_path):
    cache = tmp_path / "not-restic"
    cache.write_bytes(b"nope\n")
    os.chmod(cache, 0o666)
    before = stat.S_IMODE(cache.stat().st_mode)
    lock = tmp_path / "restic.lock"
    lock.write_text(
        "version=0.19.1\n"
        "archive=restic_0.19.1_linux_amd64.bz2\n"
        "url=\n"
        f"sha256={'ab' * 32}\n"
        f"binary_sha256={'cd' * 32}\n"
        "cache_name=restic_0.19.1_linux_amd64\n"
    )
    result = _run(lock, tmp_path / "tools", cache)
    assert result.returncode != 0
    assert not (tmp_path / "tools" / "restic").exists()
    assert stat.S_IMODE(cache.stat().st_mode) == before
