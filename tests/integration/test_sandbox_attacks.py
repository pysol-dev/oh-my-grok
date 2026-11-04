"""Momus round 4 (P1/P2/S1, R1) through the REAL Engine flow: candidate code runs only inside the bwrap sandbox.

Each payload aims at the real trusted surfaces (venv site-packages, omo_grok/verifier.py) and at this test's own
OMO_HOME DB and secrets. Every verification must fail, the trusted files and the DB must be untouched, and the job
must not reach done. Run with the static scan ON (rejected before running) and OFF (the sandbox alone holds)."""
import base64
import json
import sqlite3
from pathlib import Path

import pytest

from attacks import payload
from helpers import SECRET_KEY
from omo_grok import fixture as fx
from omo_grok import integrity, sandbox, verifier
from omo_grok.paths import CODE_ROOT, get_paths

SITE = sorted(str(p) for p in (CODE_ROOT / ".venv" / "lib").glob("python*/site-packages"))
VERIFIER_SRC = CODE_ROOT / "omo_grok" / "verifier.py"
PTH = [Path(s) / "zz_omo_p1_probe.pth" for s in SITE]


@pytest.fixture(autouse=True)
def _restore_trusted_files():
    """If the sandbox ever failed, undo the damage (the test still fails on its own assertions)."""
    src = VERIFIER_SRC.read_bytes()
    yield
    for p in PTH:
        if p.exists():
            p.unlink()
    if VERIFIER_SRC.read_bytes() != src:
        VERIFIER_SRC.write_bytes(src)


def _targets():
    paths = get_paths()
    return {"site": SITE, "verifier": [str(VERIFIER_SRC)], "db": [str(paths.db_path)],
            "secrets": [str(paths.secrets_dir)]}


def _plant(steps):
    def fix(wt: Path) -> str:
        (wt / "calc" / "__init__.py").write_text(payload(steps, _targets()))
        return fx.commit_all(wt, f"attack {'+'.join(steps)}")
    return fix


def _db_fingerprint(e, job):
    rows = {}
    for tbl in ("jobs", "approvals", "plan_revisions", "reviews", "gate_results", "role_bindings"):
        rows[tbl] = [tuple(r) for r in e.conn.execute(f"SELECT * FROM {tbl} ORDER BY rowid").fetchall()]
    rows["tables"] = sorted(r[0] for r in e.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    return rows


def _artifact_text(e, job):
    root = get_paths().artifacts_dir(job)
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*.txt"))


@pytest.mark.parametrize("scan", ["on", "off"])
@pytest.mark.parametrize("steps", [["P1"], ["P2"], ["S1"], ["P1", "P2", "S1"]], ids=lambda s: "+".join(s))
def test_round4_attacks_fail_and_touch_nothing(harness, monkeypatch, steps, scan):
    if scan == "off":
        monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    trusted_before = integrity.snapshot()
    phase_before = e.status(job)["phase"]
    before = _db_fingerprint(e, job)
    _, sha, res = harness.implement(job, fix=_plant(steps))
    v = res["verification"]
    assert v["status"] == "failed", v
    if scan == "off":  # it really ran, inside the sandbox, and saw nothing
        assert v["sandbox"] == "bwrap" and "ORACLE" in v["detail"], v
        err = _artifact_text(e, job)
        assert "ATTACK" in err and "WROTE" not in err and "PATCHED" not in err and "DB WRITTEN" not in err, err[-3000:]
        assert " SECRET " not in err
    # trusted files untouched
    assert integrity.diff(trusted_before, integrity.snapshot()) == []
    assert not any(p.exists() for p in PTH)
    # DB untouched except the failed verification and the repair bookkeeping it causes
    after = _db_fingerprint(e, job)
    assert "pwned" not in after["tables"]
    assert after["approvals"] == before["approvals"] and after["role_bindings"] == before["role_bindings"]
    assert after["gate_results"] == before["gate_results"] == []
    s = e.status(job)
    assert s["phase"] == phase_before == "running" and s["phase"] != "done"
    assert not any(ev["kind"] == "tamper" for ev in e.status(job, include_events=True)["events"])
    # no secret (raw or base64) reached any stored artifact
    text = _artifact_text(e, job)
    assert SECRET_KEY not in text and base64.b64encode(SECRET_KEY.encode()).decode()[:20] not in text
    vrow = e._one("SELECT * FROM verifications WHERE revision=?", (sha,))
    assert vrow["status"] == "failed" and vrow["sandbox"] in ("bwrap", "none")
    assert json.loads(vrow["tamper_json"]) == []


def test_p1_poison_does_not_persist_into_the_next_run(harness, monkeypatch):
    """Cycle 1 tries to install a .pth that swaps in a correct calc; cycle 2 is a plain wrong candidate. Under the
    old design the second run passed. Here it must fail too."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    _, _, r1 = harness.implement(job, fix=_plant(["P1"]))
    assert r1["verification"]["status"] == "failed"
    _, _, r2 = harness.implement(job, fix=lambda wt: fx.commit_all(wt, "no-op retry"))
    assert r2["verification"]["status"] == "failed", r2["verification"]
    assert e.status(job)["phase"] != "done" and not any(p.exists() for p in PTH)


def test_s1_full_job_cannot_reach_done_and_db_is_unchanged(harness, monkeypatch):
    """The S1 payload runs (scan off) through the real Engine flow; afterwards the only DB changes are the failed
    verification and its repair bookkeeping. The job is not done and no gate ran."""
    monkeypatch.setattr(verifier, "scope_problems", lambda *a, **k: [])
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    db = get_paths().db_path
    con = sqlite3.connect(db)
    before_jobs = con.execute("SELECT id, phase FROM jobs").fetchall()
    con.close()
    _, sha, res = harness.implement(job, fix=_plant(["S1"]))
    assert res["verification"]["status"] == "failed"
    con = sqlite3.connect(db)
    assert con.execute("SELECT id, phase FROM jobs").fetchall() == before_jobs
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='pwned'").fetchone()
    assert con.execute("SELECT COUNT(*) FROM gate_results").fetchone()[0] == 0
    kinds = {r[0] for r in con.execute("SELECT kind FROM events WHERE job_id=? AND id > (SELECT MAX(id) FROM events "
                                       "WHERE job_id=? AND kind='approval_recorded')", (job, job))}
    con.close()
    assert "final_gate" not in kinds and "tamper" not in kinds and "verification" in kinds
    assert e.status(job)["phase"] != "done"


def test_no_sandbox_fails_closed_and_never_runs_candidate(harness, monkeypatch):
    monkeypatch.setattr(sandbox, "probe", lambda: sandbox.Probe(False, "none", "no sandbox available: test"))
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    _, sha, res = harness.implement(job)  # even the REAL fix cannot pass without a sandbox
    v = res["verification"]
    assert v["status"] == "error" and "no sandbox available" in v["detail"] and "fail closed" in v["detail"]
    assert v["sandbox"] == "none"
    t = harness.task(job, "implement")
    assert t["status"] == "blocked"
    assert e.status(job)["phase"] != "done"


def test_tamper_during_verification_fails_and_raises_event(harness, monkeypatch):
    """Defence in depth: if a trusted file changes while candidate code runs, the run fails with a tamper event."""
    e = harness.e
    job = harness.submit()["job_id"]
    harness.approve(job, harness.to_approval(job))
    real = verifier._run_python
    probe_file = get_paths().config_dir / "zz_tamper_probe.json"

    def evil(*a, **k):
        out = real(*a, **k)
        probe_file.write_text("{}")  # simulates a successful escape that dropped a file into a trusted surface
        return out
    monkeypatch.setattr(verifier, "_run_python", evil)
    try:
        _, sha, res = harness.implement(job)  # the real fix, which would otherwise pass
    finally:
        probe_file.unlink(missing_ok=True)
    v = res["verification"]
    assert v["status"] == "failed" and v["detail"].startswith("TAMPER") and v["tamper"]
    evs = e.status(job, include_events=True)["events"]
    assert any(ev["kind"] == "tamper" for ev in evs)
    assert e.status(job)["phase"] != "done"


def test_sandbox_view_is_minimal(tmp_path):
    """What a candidate process can see: no state/, omo_grok/, venv, /home, /workspace; no network; ro runtime."""
    pr = sandbox.probe()
    assert pr.ok and pr.mechanism == "bwrap", pr
    work = tmp_path / "work"
    work.mkdir()
    code = ("import os, socket, json\n"
            "r = {'root': sorted(os.listdir('/')), 'workspace': os.path.exists('/workspace'),"
            " 'home': os.path.exists('/home'), 'usr_local': os.listdir('/usr/local'), 'env': dict(os.environ)}\n"
            "try:\n    socket.create_connection(('1.1.1.1', 80), 2); r['net'] = True\n"
            "except OSError:\n    r['net'] = False\n"
            "try:\n    open('/usr/lib/x', 'w'); r['usr_writable'] = True\n"
            "except OSError:\n    r['usr_writable'] = False\n"
            "open('/work/ok', 'w').write('1')\n"
            "print(json.dumps(r))\n")
    argv = sandbox.wrap(pr, ["/usr/bin/python3", "-I", "-S", "-c", code], work, None, ".", {"PATH": "/usr/bin"})
    import subprocess
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    r = json.loads(cp.stdout)
    assert set(r["root"]) <= {"usr", "bin", "lib", "lib64", "sbin", "proc", "dev", "tmp", "work"}
    assert not r["workspace"] and not r["home"] and r["usr_local"] == []
    assert r["net"] is False and r["usr_writable"] is False and {k: v for k, v in r["env"].items() if k not in ("PWD", "LC_CTYPE")} == {"PATH": "/usr/bin"}
    assert (work / "ok").read_text() == "1"


@pytest.mark.parametrize("blob", [
    b"[" * 200_000 + b"]" * 200_000,                  # RecursionError
    b"\xff\xfe\xfa not utf-8",                        # decode error
    b'{"a": ' + b"9" * 10_000 + b"}",                 # int digit limit -> ValueError
    b"x" * (verifier.MAX_LINE + 10),                  # oversize
    b'{"id": 1, "value": NaN}', b"", b"null", b"\x00",
])
def test_r1_untrusted_child_output_fails_closed(blob):
    assert not isinstance(verifier.parse_json_untrusted(blob), dict)
    rep = verifier._parse_report(b"noise\n" + verifier.REPORT_PREFIX.encode() + blob + b"\n")
    assert rep is None or not verifier.evaluate_report(rep, 0, {"expected_tests": ["t"]}) == []
