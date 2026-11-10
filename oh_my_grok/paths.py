"""Filesystem layout. OMG_HOME overrides the runtime root (tests use temp dirs).

Legacy OMO_* environment variables are still read when the OMG_* name is unset or empty,
so a host mid-cutover keeps working.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent


def env_get(suffix: str, default: str | None = None) -> str | None:
    """Prefer OMG_<suffix>. Fall back to legacy OMO_<suffix>. Empty values are skipped."""
    for prefix in ("OMG_", "OMO_"):
        val = os.environ.get(prefix + suffix)
        if val:
            return val
    return default


def python_path() -> str:
    """Interpreter used by verifier profiles and the MCP command: OMG_PYTHON, else OMO_PYTHON, else the venv."""
    return env_get("PYTHON") or str(CODE_ROOT / ".venv" / "bin" / "python")


def tools_dir() -> Path:
    """Where scripts/bootstrap.sh installs language servers (never the runtime venv)."""
    for key in ("OMG_TOOLS_DIR", "OMG_TOOLS", "OMO_TOOLS_DIR", "OMO_TOOLS"):
        val = os.environ.get(key)
        if val:
            return Path(val)
    return CODE_ROOT / "tools"


def _tool_glob(pattern: str, missing: str) -> str:
    hits = sorted(tools_dir().glob(pattern))
    return str(hits[0]) if hits else str(tools_dir() / missing)


def node_path() -> str:
    """The node binary shipped inside the bootstrap-installed nodejs-wheel (a path that does not exist if absent,
    so the profile fails closed with missing_tool instead of falling back to some other node)."""
    return _tool_glob("venv/lib/python3*/site-packages/nodejs_wheel/bin/node", "venv/MISSING-nodejs_wheel/node")


def site_packages() -> str:
    return _tool_glob("venv/lib/python3*/site-packages", "venv/MISSING-site-packages")


def mcp_command() -> str:
    py = python_path()
    if not os.path.exists(py):
        py = sys.executable
    return f"{py} {CODE_ROOT / 'oh_my_grok' / 'mcp_server.py'}"


@dataclass(frozen=True)
class Paths:
    root: Path
    config_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def db_path(self) -> Path:
        """New homes use omg.sqlite3. An existing omo.sqlite3 is opened when the new file is absent."""
        preferred = self.state_dir / "omg.sqlite3"
        legacy = self.state_dir / "omo.sqlite3"
        if preferred.exists() or not legacy.exists():
            return preferred
        return legacy

    @property
    def secrets_dir(self) -> Path:
        return self.state_dir / "secrets"

    @property
    def hooks_path(self) -> Path:
        return self.secrets_dir / "hooks.json"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def artifacts_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "artifacts"

    def ensure(self) -> None:
        for d in (self.state_dir, self.jobs_dir, self.backups_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets_dir, 0o700)


def get_paths() -> Paths:
    root = Path(env_get("HOME") or str(CODE_ROOT)).resolve()
    cfg = Path(env_get("CONFIG_DIR") or str(CODE_ROOT / "config")).resolve()
    return Paths(root=root, config_dir=cfg)
