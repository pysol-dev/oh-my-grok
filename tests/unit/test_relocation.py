"""Stage 0: nothing shipped may depend on the host location /workspace/omo (spec/ADAPTIVE.md SUP-8)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omo_grok import config
from omo_grok.paths import CODE_ROOT

HOST_PATH = "/workspace" + "/omo"
SCANNED_SUFFIXES = {".py", ".json", ".txt", ".sh", ".lock", ".in", ""}


def _shipped_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=CODE_ROOT, capture_output=True, text=True).stdout
    files = [CODE_ROOT / p for p in out.split("\0") if p]
    if not files:
        files = [p for p in CODE_ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts and "tools" not in p.parts]
    return files


def test_no_shipped_code_or_config_hardcodes_the_host_location():
    offenders = []
    for f in _shipped_files():
        rel = f.relative_to(CODE_ROOT)
        if rel.parts[0] in ("spec", "docs", "routines", "agents") or f.suffix == ".md":
            continue  # prose documents may name the documented host location
        if f.suffix not in SCANNED_SUFFIXES or f.name == "test_relocation.py":
            continue
        try:
            if HOST_PATH in f.read_text():
                offenders.append(str(rel))
        except (UnicodeDecodeError, OSError):
            continue
    assert offenders == []


def test_placeholders_expand_from_code_root_and_env(tmp_path, monkeypatch):
    (tmp_path / "verifiers.json").write_text(json.dumps({"profiles": {"p": {
        "argv": ["${OMO_PYTHON}", "-I", "${OMO_CODE_ROOT}/x.py"], "cwd": ".", "env_allow": [],
        "timeout_seconds": 5}}}))
    monkeypatch.delenv("OMO_PYTHON", raising=False)
    argv = config.load_verifier_profiles(tmp_path)["p"]["argv"]
    assert argv == [str(CODE_ROOT / ".venv" / "bin" / "python"), "-I", f"{CODE_ROOT}/x.py"]
    monkeypatch.setenv("OMO_PYTHON", "/opt/py/bin/python")
    assert config.load_verifier_profiles(tmp_path)["p"]["argv"][0] == "/opt/py/bin/python"


def test_unknown_placeholder_is_refused(tmp_path):
    (tmp_path / "verifiers.json").write_text(json.dumps({"profiles": {"p": {"argv": ["${NOPE}/x"]}}}))
    with pytest.raises(Exception) as ei:
        config.load_verifier_profiles(tmp_path)
    assert "NOPE" in str(ei.value)


def test_shipped_fixture_profile_validates_in_this_checkout():
    prof = config.load_verifier_profiles(CODE_ROOT / "config")["fixture-calc-unittest"]
    assert config.validate_profile("fixture-calc-unittest", prof) == []
    assert prof["argv"][-1] == str(config.UNITTEST_CHILD)


def test_mcp_command_is_computed_not_stored():
    out = subprocess.run([sys.executable, "-m", "omo_grok.cli", "mcp-command"], cwd=CODE_ROOT, capture_output=True,
                         text=True, env={"PYTHONPATH": str(CODE_ROOT), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    cmd = out.stdout.strip()
    assert cmd.endswith(str(CODE_ROOT / "omo_grok" / "mcp_server.py"))
    assert cmd.startswith("/")
    assert Path(cmd.split()[0]).name.startswith("python")
