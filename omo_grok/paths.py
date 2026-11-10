"""Filesystem layout. OMO_HOME overrides the runtime root (tests use temp dirs)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent


def python_path() -> str:
    """Interpreter used by verifier profiles and the MCP command: OMO_PYTHON, else the code root's own venv."""
    return os.environ.get("OMO_PYTHON") or str(CODE_ROOT / ".venv" / "bin" / "python")


def tools_dir() -> Path:
    """Where scripts/bootstrap.sh installs language servers (never the runtime venv)."""
    return Path(os.environ.get("OMO_TOOLS_DIR") or CODE_ROOT / "tools")


def mcp_command() -> str:
    py = python_path()
    if not os.path.exists(py):
        py = sys.executable
    return f"{py} {CODE_ROOT / 'omo_grok' / 'mcp_server.py'}"


@dataclass(frozen=True)
class Paths:
    root: Path
    config_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "omo.sqlite3"

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
    root = Path(os.environ.get("OMO_HOME", str(CODE_ROOT))).resolve()
    cfg = Path(os.environ.get("OMO_CONFIG_DIR", str(CODE_ROOT / "config"))).resolve()
    return Paths(root=root, config_dir=cfg)
