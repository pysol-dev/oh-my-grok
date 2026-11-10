"""Boulder projection: upstream-shaped schema, checklist grammar, deterministic bytes, never regress, tamper repair,
no import (spec/ADAPTIVE.md section 6, R5, R6)."""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from omo_grok import db, projection
from omo_grok.paths import get_paths

BASE = {"files_estimate": 1, "new_dependency": False, "external_api": False, "security_sensitive": False,
        "cross_cutting": False}


def _enable(modes):
    cfg = Path(os.environ["OMO_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol["features"] = modes
    (cfg / "policy.json").write_text(json.dumps(pol))


@pytest.fixture
def h(omo_home):
    from helpers import Harness
    _enable({"boulder": "on"})
    hh = Harness()
    yield hh
    hh.close()


def paths_for(job):
    d = get_paths().job_dir(job) / ".omo"
    return d / "boulder.json", d


def load(job):
    return json.loads(paths_for(job)[0].read_text())


def gen(h, job):
    return h.e._one("SELECT MAX(id) m FROM events WHERE job_id=?", (job,))["m"]


def test_submit_projects_an_upstream_shaped_boulder(h):
    job = h.submit()["job_id"]
    b = load(job)
    assert b["schema_version"] == 2 and b["active_work_id"] == job
    w = b["works"][job]
    for k in ("work_id", "active_plan", "plan_name", "status", "started_at", "updated_at", "session_ids",
              "session_origins", "agent", "task_sessions"):
        assert k in w, k
    assert w["status"] == "active" and w["work_id"] == job
    for k in ("active_plan", "started_at", "status", "session_ids", "plan_name", "agent", "task_sessions"):
        assert b[k] == w[k], k  # mirrored root fields exactly as upstream
    assert Path(w["active_plan"]).is_file() and Path(w["active_plan"]).name == w["plan_name"] + ".md"
    x = b["x_omo_grok"]
    assert x["job_id"] == job and x["generation"] == gen(h, job)
    assert x["projection"] == "derived-from-sqlite" and x["resume"] == {"tool": "omo.status", "job_id": job}
    assert x["phase"] == "research" and x["next_action"]
    d = paths_for(job)[1]
    assert (d / ".gitignore").read_text().strip() == "*"
    assert oct(paths_for(job)[0].stat().st_mode & 0o777) == "0o600"


def test_claims_become_sessions_with_engine_issued_ids(h):
    job = h.submit()["job_id"]
    pkt = h.claim(job, "explore")
    b = load(job)
    w = b["works"][job]
    token = f"omo-grok:{h.e._one('SELECT bot_id FROM role_bindings WHERE role=?', ('explorer',))['bot_id']}:{pkt['attempt_id']}"
    assert w["session_ids"] == [token]
    (ts,) = w["task_sessions"].values()
    assert ts["session_id"] == token and ts["status"] == "running" and ts["category"] == "explorer"
    assert ts["task_label"] == "explore"
    h.e.complete(job, pkt["task_id"], pkt["attempt_id"],
                 {"repo_map": "m", "files": ["a:1"], "tests": [], "constraints": [], "questions": []},
                 [{"kind": "explore", "name": "e.md", "content": "E"}])
    (ts,) = load(job)["works"][job]["task_sessions"].values()
    assert ts["status"] == "completed" and ts["ended_at"]


def test_hephaestus_without_bot_binding_uses_the_role_in_the_session_id(h):
    job = h.submit()["job_id"]
    h.research(job)
    h.analyze(job)
    pkt = h.claim(job, "plan")
    assert f"omo-grok:hephaestus:{pkt['attempt_id']}" in load(job)["works"][job]["session_ids"]


CHECK = re.compile(r"^- \[( |x|~)\] (\d+|F[1-4])\. (.+)$")


def test_plan_markdown_uses_the_verified_checklist_grammar(h):
    job = h.submit()["job_id"]
    h.to_approval(job, "THE PLAN BODY\n- [ ] 99. this line is plan text, inside a fence")
    md = Path(load(job)["active_plan"]).read_text()
    lines = md.splitlines()
    assert "## TODOs" in lines and "## Final Verification Wave" in lines
    todo = lines[lines.index("## TODOs") + 1:lines.index("## Final Verification Wave")]
    rows = [CHECK.match(l) for l in todo if l.startswith("- [")]
    assert rows and all(rows)
    kinds = [r.group(3).split()[0] for r in rows]
    assert kinds == ["explore", "research", "analyze", "plan", "plan_review"]
    assert [r.group(1) for r in rows] == ["x"] * 5
    fin = lines[lines.index("## Final Verification Wave") + 1:]
    ff = [CHECK.match(l) for l in fin if l.startswith("- [") and not l.startswith("    ")]
    assert [m.group(2) for m in ff[:4]] == ["F1", "F2", "F3", "F4"] and all(m.group(1) == " " for m in ff[:4])
    h_line = [l for l in lines if l.startswith("## Plan (hash ")]
    assert h_line and h_line[0].endswith(h.e.plan_show(job)["current"]["plan_hash"] + ")")
    assert "THE PLAN BODY" in md
    plan_part = md[md.index("## Plan (hash "):]
    assert "```" in plan_part  # the plan text is fenced so its own checklist-looking lines are not tasks


def test_final_verification_wave_ticks_as_evidence_arrives(h):
    job, sha, res = h.full_fixture_job()
    lines = Path(load(job)["active_plan"]).read_text().splitlines()
    fin = [CHECK.match(l) for l in lines if l.startswith("- [") and "F" in l[:9]]
    assert [m.group(1) for m in fin if m.group(2).startswith("F")] == ["x", "x", "x", "x"]
    assert load(job)["works"][job]["status"] == "completed"
    assert load(job)["active_work_id"] is None


def test_blocked_marks_the_task_and_pauses_the_work(h):
    job = h.submit()["job_id"]
    pkt = h.claim(job, "explore")
    h.e.complete(job, pkt["task_id"], pkt["attempt_id"], {"status": "blocked", "reason": "cannot read repo"})
    b = load(job)
    assert b["works"][job]["status"] == "paused" and b["active_work_id"] == job
    lines = Path(b["active_plan"]).read_text().splitlines()
    assert any(l.startswith("- [~] 1. explore") for l in lines)


def test_cancelled_is_abandoned(h):
    job = h.submit()["job_id"]
    h.e.cancel(job, "no longer needed", actor="t")
    w = load(job)["works"][job]
    assert w["status"] == "abandoned" and w["ended_at"] and load(job)["active_work_id"] is None


def test_unchanged_state_is_not_rewritten_and_bytes_are_deterministic(h):
    job = h.submit()["job_id"]
    f = paths_for(job)[0]
    before = (f.read_bytes(), f.stat().st_ino)
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] == "unchanged"
    assert (f.read_bytes(), f.stat().st_ino) == before
    assert before[0] == (json.dumps(json.loads(before[0]), sort_keys=True, indent=2) + "\n").encode()


def test_projection_record_tracks_generation_and_hash(h):
    job = h.submit()["job_id"]
    rec = h.e._one("SELECT * FROM boulder_projection WHERE job_id=?", (job,))
    f = paths_for(job)[0]
    assert rec["status"] == "ok" and rec["generation"] == gen(h, job)
    assert rec["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest() and rec["pending_sha256"] is None


def test_never_regresses_when_a_newer_trusted_file_exists(h):
    job = h.submit()["job_id"]
    f = paths_for(job)[0]
    data = json.loads(f.read_text())
    data["x_omo_grok"]["generation"] = gen(h, job) + 1000
    newer = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    f.write_bytes(newer)
    with db.tx(h.e.conn):
        h.e.conn.execute("UPDATE boulder_projection SET sha256=?, generation=? WHERE job_id=?",
                         (hashlib.sha256(newer).hexdigest(), gen(h, job) + 1000, job))
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] == "skipped_newer" and f.read_bytes() == newer


@pytest.mark.parametrize("damage", ["edit", "truncate", "garbage", "delete", "huge_generation"])
def test_tampering_is_repaired_and_logged_never_imported(h, damage):
    job = h.submit()["job_id"]
    f = paths_for(job)[0]
    good = f.read_bytes()
    phase_before = h.e._job(job)["phase"]
    if damage == "edit":
        d = json.loads(good)
        d["works"][job]["status"] = "completed"
        f.write_text(json.dumps(d))
    elif damage == "truncate":
        f.write_bytes(good[:40])
    elif damage == "garbage":
        f.write_bytes(b"\x00\xff not json")
    elif damage == "delete":
        f.unlink()
    else:
        d = json.loads(good)
        d["x_omo_grok"]["generation"] = 10 ** 12
        f.write_text(json.dumps(d))
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] == "written" and r["tamper"] is True
    assert json.loads(f.read_bytes())["works"][job]["status"] == "active"
    assert h.e._job(job)["phase"] == phase_before  # the file never changes job state
    ev = h.e._all("SELECT data_json FROM events WHERE job_id=? AND kind='boulder_tamper'", (job,))
    assert len(ev) == 1
    after = load(job)
    assert after["x_omo_grok"]["generation"] == gen(h, job)  # includes the tamper event itself
    assert projection.project(h.e.conn, h.e.paths, job)["status"] == "unchanged"


def test_editing_the_file_never_alters_decisions(h):
    job = h.submit()["job_id"]
    f = paths_for(job)[0]
    d = json.loads(f.read_text())
    d["works"][job]["status"] = "completed"
    d["x_omo_grok"]["phase"] = "done"
    f.write_text(json.dumps(d))
    before = h.e.status(job)
    assert before["phase"] == "research"
    h.research(job)  # engine carries on from the database
    assert h.e.status(job)["phase"] == "analysis"


@pytest.mark.parametrize("name", ["boulder.json", "boulder.lock"])
def test_symlink_is_replaced_without_being_followed(h, tmp_path, name):
    job = h.submit()["job_id"]
    d = paths_for(job)[1]
    target = d / name
    secret = tmp_path / "secret-target"
    secret.write_text("do-not-touch")
    target.unlink()
    target.symlink_to(secret)
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] == "written" and r["tamper"] is True
    assert target.is_file() and not target.is_symlink()
    assert secret.read_text() == "do-not-touch"
    if name == "boulder.json":
        assert json.loads(target.read_text())["works"][job]["status"] == "active"
    assert h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='boulder_tamper'", (job,))


def test_symlinked_omo_directory_is_replaced_without_being_followed(h, tmp_path):
    job = h.submit()["job_id"]
    omo = paths_for(job)[1]
    outside = tmp_path / "outside-omo"
    outside.mkdir()
    (outside / "marker").write_text("untouched")
    (outside / "boulder.json").write_text("secret-boulder")
    (outside / "boulder.lock").write_text("secret-lock")
    shutil.rmtree(omo)
    omo.symlink_to(outside, target_is_directory=True)
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] == "written" and r["tamper"] is True
    assert omo.is_dir() and not omo.is_symlink()
    assert (outside / "marker").read_text() == "untouched"
    assert (outside / "boulder.json").read_text() == "secret-boulder"
    assert (outside / "boulder.lock").read_text() == "secret-lock"
    assert not (outside / "plans").exists()
    assert not (outside / ".gitignore").exists()
    assert json.loads((omo / "boulder.json").read_text())["works"][job]["status"] == "active"
    assert h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='boulder_tamper'", (job,))


def test_symlinked_plans_directory_is_replaced_without_being_followed(h, tmp_path):
    job = h.submit()["job_id"]
    omo = paths_for(job)[1]
    plans = omo / "plans"
    outside = tmp_path / "outside-plans"
    outside.mkdir()
    plan_leaf = f"omo-{job[:8]}.md"
    (outside / "marker").write_text("untouched")
    (outside / plan_leaf).write_text("secret-plan")
    shutil.rmtree(plans)
    plans.symlink_to(outside, target_is_directory=True)
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] == "written" and r["tamper"] is True
    assert plans.is_dir() and not plans.is_symlink()
    assert (outside / "marker").read_text() == "untouched"
    assert (outside / plan_leaf).read_text() == "secret-plan"
    assert sorted(p.name for p in outside.iterdir()) == ["marker", plan_leaf]
    assert (plans / plan_leaf).is_file() and "secret-plan" not in (plans / plan_leaf).read_text()
    assert h.e._one("SELECT 1 FROM events WHERE job_id=? AND kind='boulder_tamper'", (job,))


def test_plan_file_edit_is_detected_and_restored(h):
    job = h.submit()["job_id"]
    h.to_approval(job, "ORIGINAL PLAN")
    plan = Path(load(job)["active_plan"])
    plan.write_text("EVIL REPLACEMENT")
    r = projection.project(h.e.conn, h.e.paths, job)
    assert "ORIGINAL PLAN" in plan.read_text() and r["tamper"] is True


def test_off_means_no_files(omo_home):
    from helpers import Harness
    hh = Harness()
    try:
        job = hh.submit()["job_id"]
        assert not (get_paths().job_dir(job) / ".omo").exists()
        assert hh.e._one("SELECT COUNT(*) c FROM boulder_projection")["c"] == 0
    finally:
        hh.close()


def test_integrity_failure_of_plan_artifact_omits_text_but_still_projects(h):
    job = h.submit()["job_id"]
    h.to_approval(job, "PLAN TEXT")
    row = h.e._one("SELECT a.rel_path FROM plan_revisions p JOIN artifacts a ON a.id=p.artifact_id WHERE p.job_id=?",
                   (job,))
    (get_paths().artifacts_dir(job) / row["rel_path"]).write_text("corrupted after the fact")
    r = projection.project(h.e.conn, h.e.paths, job)
    assert r["status"] in ("written", "unchanged")
    md = Path(load(job)["active_plan"]).read_text()
    assert "PLAN TEXT" not in md and "corrupted after the fact" not in md and "integrity" in md.lower()
