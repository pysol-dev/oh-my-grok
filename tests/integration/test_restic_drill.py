"""Disposable restic drill. Skips when the pinned binary is not installed."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_drill_round_trip_and_stale_lock_stay_in_tmp():
    import pytest
    restic = ROOT / "tools" / "restic"
    if not restic.is_file():
        pytest.skip("restic 0.19.1 is not installed")
    proc = subprocess.run([str(ROOT / "scripts" / "drill-restic.sh")], capture_output=True, text=True,
                          timeout=180, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "drill ok" in proc.stdout
    assert "OMG_RESTIC_PASSWORD" not in proc.stdout
