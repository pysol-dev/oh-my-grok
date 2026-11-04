import json
import shutil
import sys
from pathlib import Path

import pytest

CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def omo_home(tmp_path, monkeypatch):
    """Isolated runtime root + config dir per test (never touches the real state/)."""
    home = tmp_path / "home"
    cfg = tmp_path / "config"
    home.mkdir()
    cfg.mkdir()
    for f in ("verifiers.json", "bots.json"):
        shutil.copy(CODE_ROOT / "config" / f, cfg / f)
    (cfg / "policy.json").write_text(json.dumps({"backoff_base_seconds": 10, "backoff_cap_seconds": 120}))
    monkeypatch.setenv("OMO_HOME", str(home))
    monkeypatch.setenv("OMO_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("OMO_ALLOW_INSECURE_HOOKS", raising=False)
    return home


@pytest.fixture
def harness(omo_home):
    from helpers import Harness
    h = Harness()
    yield h
    h.close()
