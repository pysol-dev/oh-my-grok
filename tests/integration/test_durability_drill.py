"""Disposable durability drill. OMG_HOME and the agent-store path are temporary.

Outgoing transport is disabled for the whole drill, including the watchdog that recover.sh
starts, so a restored hooks.json cannot open a socket.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from oh_my_grok.engine import Engine
from oh_my_grok.hooks import set_hook
from oh_my_grok.paths import get_paths
from oh_my_grok.transport import HttpResult

ROOT = Path(__file__).resolve().parents[2]
OMG = str(ROOT / "omg")
RECOVER = str(ROOT / "scripts" / "recover.sh")
HOOK_URL = "https://drill.invalid/hook/ZZDRILLPATHTOKEN"
HOOK_KEY = "hk_DRILL_SENTINEL_0123456789abcdef"


def _absent(text: str) -> None:
    for needle in (HOOK_URL, HOOK_KEY, "ZZDRILLPATHTOKEN", "hk_DRILL_SENTINEL"):
        if needle in text:
            raise AssertionError("secret material appeared in command output")


def _first_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise AssertionError("recover produced no json")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(args, env, timeout=900):
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout, check=False)


def test_wipe_and_recover_is_idempotent_and_fails_closed(omg_home, tmp_path, monkeypatch):
    store = tmp_path / "agent-store"
    passphrase = "drill-passphrase-" + hashlib.sha256(b"throwaway").hexdigest()[:16]
    monkeypatch.setenv("OMG_BACKUP_PASSPHRASE", passphrase)
    monkeypatch.setenv("OMG_BACKUP_AGENT_STORE", str(store))
    monkeypatch.setenv("OMG_DISABLE_TRANSPORT", "1")
    monkeypatch.setenv("OMG_HOME", str(omg_home))
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    policy = json.loads((cfg / "policy.json").read_text())
    policy["features"] = {"backup": "off", "watchdog": "off"}
    policy["inline_dispatch"] = False
    policy["backup_targets"] = [{"type": "agent_store", "path": str(store)}]
    (cfg / "policy.json").write_text(json.dumps(policy))
    calls = []

    def transport(*args, **kwargs):
        calls.append(args)
        return HttpResult(None, None, "transport disabled")

    engine = Engine(transport=transport)
    try:
        job = engine.submit("Fix calc.add in the synthetic fixture", "durability-drill-0001", mode="fixture",
                            acceptance_criteria=["unittest suite exits 0"])["job_id"]
    finally:
        engine.close()
    assert calls == []
    set_hook(get_paths(), "explorer", HOOK_URL, HOOK_KEY)
    log_path = get_paths().logs_dir / "drill.log"
    log_path.write_text("drill-log-line\n")
    hooks_hash = _sha(get_paths().hooks_path)
    log_hash = _sha(log_path)
    env = os.environ.copy()

    def stop():
        _run([OMG, "watchdog", "stop"], env, timeout=30)

    try:
        init = _run([OMG, "backup", "init-key", "--json"], env, timeout=180)
        assert init.returncode == 0, init.stderr
        _absent(init.stdout + init.stderr)
        if passphrase in init.stdout or passphrase in init.stderr:
            raise AssertionError("secret material appeared in command output")
        backed = _run([OMG, "backup", "--json"], env, timeout=180)
        assert backed.returncode == 0, backed.stderr
        _absent(backed.stdout + backed.stderr)
        if passphrase in backed.stdout or passphrase in backed.stderr:
            raise AssertionError("secret material appeared in command output")
        bundles = list((store / "bundles").glob("omg-*.tar.gpg"))
        assert len(bundles) == 1
        original = bundles[0].read_bytes()
        original_sig = Path(str(bundles[0]) + ".sig").read_bytes()
        assert (store / "kit" / "encryption.sec.asc").is_file()
        assert (store / "kit" / "signing.pub").is_file()

        shutil.rmtree(omg_home / "state")
        shutil.rmtree(omg_home / "jobs")
        (cfg / "policy.json").unlink()
        assert not (omg_home / "state").exists()

        first = _run(["bash", RECOVER], env)
        assert first.returncode == 0, first.stderr
        _absent(first.stdout + first.stderr)
        if passphrase in first.stdout or passphrase in first.stderr:
            raise AssertionError("secret material appeared in command output")
        db_path = get_paths().db_path
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        assert count > 0
        assert _sha(get_paths().hooks_path) == hooks_hash
        assert (get_paths().hooks_path.stat().st_mode & 0o777) == 0o600
        assert _sha(get_paths().logs_dir / "drill.log") == log_hash
        assert (get_paths().jobs_dir / job).is_dir()
        manifest = json.loads(Path(str(bundles[0]) + ".manifest.json").read_text())
        for rel, meta in manifest["files"].items():
            if rel.endswith(".sqlite3"):
                continue
            if rel == "config/policy.json":
                restored = cfg / "policy.json"
            else:
                restored = omg_home / rel
            assert restored.is_file(), rel
            assert hashlib.sha256(restored.read_bytes()).hexdigest() == meta["sha256"]
        stop()

        digest = _sha(db_path)
        second = _run(["bash", RECOVER], env)
        assert second.returncode == 0, second.stderr
        assert _first_json(second.stdout)["action"] == "noop"
        assert _sha(db_path) == digest
        assert _sha(get_paths().hooks_path) == hooks_hash
        stop()

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO events (ts, kind, data_json) VALUES ('t', 'drill_note', '{\"n\":1}')")
        conn.commit()
        conn.close()
        newer = _run(["bash", RECOVER], env)
        assert newer.returncode == 0, newer.stderr
        body = _first_json(newer.stdout)
        assert body["action"] == "noop" and body["relation"] == "local_newer"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='drill_note'").fetchone()[0] == 1
        finally:
            conn.close()
        stop()

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE events SET data_json='{\"diverged\":1}' WHERE id=(SELECT MIN(id) FROM events)")
        conn.commit()
        mutated = conn.execute("SELECT data_json FROM events ORDER BY id ASC LIMIT 1").fetchone()[0]
        conn.close()
        diverged = _run(["bash", RECOVER], env)
        assert diverged.returncode == 11, diverged.stdout + diverged.stderr
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT data_json FROM events ORDER BY id ASC LIMIT 1").fetchone()[0] == mutated
        finally:
            conn.close()
        stop()

        db_path.write_bytes(b"this is not sqlite")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        restored = _run(["bash", RECOVER], env)
        assert restored.returncode == 0, restored.stderr
        assert _first_json(restored.stdout)["relation"] == "corrupt"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='drill_note'").fetchone()[0] == 0
        finally:
            conn.close()
        assert _sha(get_paths().hooks_path) == hooks_hash
        stop()

        digest = _sha(db_path)
        flipped = bytearray(original)
        flipped[-1] ^= 0x5A
        bundles[0].write_bytes(bytes(flipped))
        # The restore installed a local copy. The agent store is the higher-ranked source, so a
        # tampered store bundle fails closed instead of falling back to the local copy.
        tampered = _run(["bash", RECOVER], env)
        assert tampered.returncode == 12, tampered.stdout + tampered.stderr
        assert _sha(db_path) == digest
        bundles[0].write_bytes(original)
        Path(str(bundles[0]) + ".sig").write_bytes(b"bad-signature")
        bad_sig = _run(["bash", RECOVER], env)
        assert bad_sig.returncode == 12, bad_sig.stdout + bad_sig.stderr
        assert _sha(db_path) == digest
        Path(str(bundles[0]) + ".sig").write_bytes(original_sig)
        _absent(tampered.stdout + tampered.stderr + bad_sig.stdout + bad_sig.stderr)
    finally:
        stop()
