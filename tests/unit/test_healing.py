"""Slice 1 failure collection: a projection of jobs, reviews, and the events log (spec/HEALING.md)."""
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from oh_my_grok import db, features
from oh_my_grok.config import canonical_json, sha256_text
from oh_my_grok.errors import GateRefused, NotFound, ValidationError
from oh_my_grok.healing import RESERVED_SOURCES, collect, show
from oh_my_grok.states import ROLES

ROOT = Path(__file__).resolve().parents[2]
OMG = str(ROOT / "omg")


def _sha(obj) -> str:
    return sha256_text(canonical_json(obj))


def _run(args, env):
    return subprocess.run([OMG, *args], capture_output=True, text=True, env=env, timeout=60)


def _policy(mode):
    path = Path(os.environ["OMG_CONFIG_DIR"]) / "policy.json"
    data = json.loads(path.read_text())
    data["features"] = {"healing": mode}
    path.write_text(json.dumps(data))


def _job(conn, *, phase="running", repair_cycles=0, failure_reason=None, blocked_from=None, mode="fixture"):
    jid = str(uuid.uuid4())
    now = db.utcnow()
    conn.execute(
        "INSERT INTO jobs(id,idempotency_key,goal,mode,requirements_json,requirements_hash,phase,blocked_from,"
        "version,policies_json,repair_cycles,plan_rounds,failure_reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,'{}',?,?,?,?,?)",
        (jid, "k-" + jid, "goal", mode, '{"goal":"g"}', "h" * 64, phase, blocked_from,
         repair_cycles, 0, failure_reason, now, now))
    return jid


def _task(conn, job_id, kind="code_review", role="code-reviewer", status="passed"):
    tid = str(uuid.uuid4())
    now = db.utcnow()
    conn.execute(
        "INSERT INTO tasks(id,job_id,kind,role,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (tid, job_id, kind, role, status, now, now))
    return tid


def _review(conn, job_id, task_id, *, typ="code", findings=None, verdict="reject", revision="ab" * 20,
            invalidated=0):
    rid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO reviews(id,job_id,task_id,type,reviewer_role,revision,input_hashes_json,findings_json,"
        "verdict,evidence_json,invalidated,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, job_id, task_id, typ, role_for(typ), revision, "[]",
         json.dumps(findings if findings is not None else ["bug: x"]), verdict, "{}", invalidated, db.utcnow()))
    return rid


def role_for(typ):
    return {"plan": "momus", "code": "code-reviewer", "qa": "qa-executor"}[typ]


def _event(conn, job_id, kind, data=None, task_id=None):
    conn.execute(
        "INSERT INTO events(ts,job_id,task_id,kind,actor,data_json) VALUES (?,?,?,?,?,?)",
        (db.utcnow(), job_id, task_id, kind, "engine", json.dumps(data) if data is not None else None))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def _dump(conn, table, order):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


@pytest.fixture
def eng(omg_home):
    from oh_my_grok.engine import Engine
    e = Engine()
    yield e
    e.close()


def test_migration_6_creates_snapshot_tables(tmp_path):
    conn = db.connect(tmp_path / "h.sqlite3")
    assert 6 in db.migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"failure_snapshots", "failure_items"} <= names


def test_healing_off_refuses_and_writes_nothing(eng):
    jid = _job(eng.conn, phase="blocked", failure_reason="disk full", blocked_from="running")
    with pytest.raises(GateRefused, match="healing is off"):
        collect(eng.conn, "off")
    with pytest.raises(ValidationError):
        collect(eng.conn, "on")
    assert _count(eng.conn, "SELECT COUNT(*) FROM failure_snapshots") == 0
    assert _count(eng.conn, "SELECT COUNT(*) FROM failure_items") == 0
    assert _count(eng.conn, "SELECT COUNT(*) FROM events WHERE kind='failures_collected'") == 0
    assert tuple(eng.conn.execute("SELECT phase, version FROM jobs WHERE id=?", (jid,)).fetchone()) == ("blocked", 1)


def test_empty_shadow_and_enforce_write_snapshots_without_enqueueing(eng):
    shadow = collect(eng.conn, "shadow")
    assert shadow["written"] is True and shadow["mode"] == "shadow"
    assert shadow["item_count"] == 0 and shadow["items"] == []
    assert shadow["sha256"] == shadow["corpus_sha256"] == _sha([])
    enforce = collect(eng.conn, "enforce")
    assert enforce["written"] is True and enforce["mode"] == "enforce"
    assert enforce["sha256"] == shadow["sha256"]
    assert enforce["snapshot_id"] != shadow["snapshot_id"]
    assert _count(eng.conn, "SELECT COUNT(*) FROM jobs") == 0
    assert _count(eng.conn, "SELECT COUNT(*) FROM tasks") == 0
    assert _count(eng.conn, "SELECT COUNT(*) FROM dispatch_outbox") == 0
    modes = [r[0] for r in eng.conn.execute("SELECT mode FROM failure_snapshots ORDER BY seq")]
    assert modes == ["shadow", "enforce"]


def _corpus(conn):
    blocked = _job(conn, phase="blocked", failure_reason="repair_budget_exhausted", blocked_from="running")
    done = _job(conn, phase="done", repair_cycles=2)
    quiet = _job(conn, phase="running")
    tid = _task(conn, done)
    review = _review(conn, done, tid, findings=["bug: x", "bug: y"], invalidated=1)
    _review(conn, quiet, _task(conn, quiet), verdict="accept", findings=[])
    e1 = _event(conn, done, "repair_cycle", {"failures": 1, "reason": "verification failed", "max": 3})
    e2 = _event(conn, done, "repair_cycle", {"failures": 2, "reason": "qa rejected", "max": 3})
    _event(conn, blocked, "github_review_bot", {"comment": "nit"})
    return {"blocked": blocked, "done": done, "quiet": quiet, "review": review, "events": (e1, e2)}


def test_sources_are_blocked_repair_review_and_repeated(eng):
    ids = _corpus(eng.conn)
    out = collect(eng.conn, "shadow")
    by_job = {}
    for item in out["items"]:
        assert item["class"] and item["job_id"]
        assert item["sha256"] == _sha(item["payload"])
        assert item["class"] == item["payload"]["class"]
        assert item["job_id"] == item["payload"]["job_id"]
        assert item["source"] != "github_review_bot"
        by_job.setdefault(item["job_id"], []).append(item)
    assert ids["quiet"] not in by_job
    blocked = by_job[ids["blocked"]]
    assert [i["class"] for i in blocked] == ["blocked"]
    assert blocked[0]["payload"]["failure_reason"] == "repair_budget_exhausted"
    assert blocked[0]["payload"]["blocked_from"] == "running"
    kinds = sorted(i["class"] for i in by_job[ids["done"]])
    assert kinds == ["repair", "repair", "repeated", "review"]
    review = next(i for i in by_job[ids["done"]] if i["class"] == "review")
    assert review["payload"]["findings"] == ["bug: x", "bug: y"]
    assert review["payload"]["review_id"] == ids["review"]
    assert review["payload"]["invalidated"] is True
    repeated = next(i for i in by_job[ids["done"]] if i["class"] == "repeated")
    assert repeated["payload"]["count"] == 2
    assert repeated["payload"]["event_ids"] == list(ids["events"])
    assert out["sha256"] == _sha([i["sha256"] for i in out["items"]])


def test_done_job_with_repair_cycles_and_no_event_still_appears(eng):
    jid = _job(eng.conn, phase="done", repair_cycles=1)
    out = collect(eng.conn, "enforce")
    items = [i for i in out["items"] if i["job_id"] == jid]
    assert len(items) == 1
    assert items[0]["class"] == "repair"
    assert items[0]["payload"]["failures"] == 1
    assert items[0]["payload"]["record"] == "jobs.repair_cycles"
    assert eng.conn.execute("SELECT phase FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "done"


def test_event_repair_is_not_duplicated_by_the_counter(eng):
    jid = _job(eng.conn, phase="done", repair_cycles=1)
    eid = _event(conn=eng.conn, job_id=jid, kind="repair_cycle", data={"failures": 1, "reason": "once", "max": 3})
    out = collect(eng.conn, "shadow")
    items = [i for i in out["items"] if i["job_id"] == jid]
    assert len(items) == 1
    assert items[0]["payload"]["event_id"] == eid
    assert "record" not in items[0]["payload"]
    assert items[0]["payload"]["reason"] == "once"


def test_second_collect_matches_item_hashes_and_ignores_its_audit_event(eng):
    _corpus(eng.conn)
    first = collect(eng.conn, "shadow")
    second = collect(eng.conn, "shadow")
    assert first["snapshot_id"] != second["snapshot_id"]
    assert first["seq"] + 1 == second["seq"]
    assert [i["sha256"] for i in first["items"]] == [i["sha256"] for i in second["items"]]
    assert first["sha256"] == second["sha256"]
    assert _count(eng.conn, "SELECT COUNT(*) FROM events WHERE kind='failures_collected'") == 2
    latest = show(eng.conn)
    assert latest["snapshot_id"] == second["snapshot_id"]
    assert [i["sha256"] for i in latest["items"]] == [i["sha256"] for i in second["items"]]
    earlier = show(eng.conn, first["snapshot_id"])
    assert earlier["snapshot_id"] == first["snapshot_id"]
    n = _count(eng.conn, "SELECT COUNT(*) FROM events")
    show(eng.conn)
    assert _count(eng.conn, "SELECT COUNT(*) FROM events") == n
    with pytest.raises(NotFound):
        show(eng.conn, "00000000-0000-4000-8000-000000000000")
    with pytest.raises(ValidationError):
        show(eng.conn, "not-a-uuid")


def test_collect_does_not_mutate_jobs_or_append_other_events(eng):
    _corpus(eng.conn)
    before = {name: _dump(eng.conn, name, order) for name, order in (
        ("jobs", "id"), ("tasks", "id"), ("reviews", "id"), ("dispatch_outbox", "id"), ("verifications", "id"))}
    events_before = _dump(eng.conn, "events", "id")
    collect(eng.conn, "enforce")
    for name, rows in before.items():
        assert _dump(eng.conn, name, "id") == rows
    events_after = _dump(eng.conn, "events", "id")
    assert events_after[:-1] == events_before
    kind_at = [row[1] for row in eng.conn.execute("PRAGMA table_info(events)")].index("kind")
    assert events_after[-1][kind_at] == "failures_collected"


def test_prior_snapshot_is_not_read_back(eng):
    jid = _job(eng.conn, phase="blocked", failure_reason="stuck", blocked_from="planning")
    first = collect(eng.conn, "shadow")
    eng.conn.execute("UPDATE failure_items SET payload_json=?, class='tampered' WHERE snapshot_id=?",
                     ('{"class":"tampered"}', first["snapshot_id"]))
    second = collect(eng.conn, "shadow")
    assert [i["sha256"] for i in second["items"]] == [i["sha256"] for i in first["items"]]
    assert second["items"][0]["class"] == "blocked"
    assert second["items"][0]["job_id"] == jid
    assert second["items"][0]["payload"]["failure_reason"] == "stuck"


def test_github_review_bot_alone_is_not_collected(eng):
    jid = _job(eng.conn, phase="done")
    _event(eng.conn, jid, "github_review_bot", {"body": "please fix"})
    out = collect(eng.conn, "shadow")
    assert out["items"] == []
    assert all(i["source"] != "github_review_bot" for i in out["items"])


def test_engine_collect_uses_policy_not_the_frozen_job_flag(eng):
    jid = _job(eng.conn, phase="blocked", failure_reason="nope", blocked_from="planning")
    assert eng._features(eng._job(jid))["healing"] == "off"
    eng.policy["features"] = features.normalize({"healing": "shadow"})
    out = eng.heal_collect()
    assert out["mode"] == "shadow"
    assert any(i["class"] == "blocked" and i["job_id"] == jid for i in out["items"])
    assert eng._dirty == set()
    assert eng._job(jid)["phase"] == "blocked"


def test_cli_off_is_a_refusal_that_writes_nothing(omg_home):
    from oh_my_grok.engine import Engine
    e = Engine()
    e.close()
    env = os.environ.copy()
    refused = _run(["heal", "collect", "--json"], env)
    assert refused.returncode == 5
    body = json.loads(refused.stdout)
    assert body["code"] == "gate_refused" and "healing is off" in body["message"]
    human = _run(["heal", "collect"], env)
    assert human.returncode == 5 and "healing is off" in human.stderr
    missing = _run(["heal", "show", "--json"], env)
    assert missing.returncode == 3
    e = Engine()
    try:
        assert _count(e.conn, "SELECT COUNT(*) FROM failure_snapshots") == 0
        assert _count(e.conn, "SELECT COUNT(*) FROM events WHERE kind='failures_collected'") == 0
    finally:
        e.close()


def test_cli_collect_and_show_print_a_blocked_fixture_snapshot(omg_home):
    from oh_my_grok.engine import Engine
    _policy("shadow")
    e = Engine()
    jid = _job(e.conn, phase="blocked", failure_reason="repair_budget_exhausted", blocked_from="running")
    version = e._job(jid)["version"]
    e.close()
    env = os.environ.copy()
    human = _run(["heal", "collect"], env)
    assert human.returncode == 0, human.stderr
    assert jid in human.stdout and "sha256:" in human.stdout and "blocked" in human.stdout
    shown = json.loads(_run(["heal", "show", "--json"], env).stdout)
    again = json.loads(_run(["heal", "collect", "--json"], env).stdout)
    assert shown["snapshot_id"] != again["snapshot_id"]
    assert [i["sha256"] for i in shown["items"]] == [i["sha256"] for i in again["items"]]
    assert any(i["class"] == "blocked" and i["job_id"] == jid for i in again["items"])
    assert len(again["sha256"]) == 64
    one = json.loads(_run(["heal", "show", shown["snapshot_id"], "--json"], env).stdout)
    assert one["snapshot_id"] == shown["snapshot_id"]
    text = _run(["heal", "show"], env)
    assert again["sha256"] in text.stdout and jid in text.stdout
    e = Engine()
    try:
        row = e._job(jid)
        assert row["phase"] == "blocked" and row["version"] == version and row["mode"] == "fixture"
    finally:
        e.close()


def test_no_healer_role_or_routine():
    assert RESERVED_SOURCES == ("github_review_bot",)
    assert "healer" not in ROLES
    names = {p.name for p in (ROOT / "routines").glob("*.md")}
    assert not any("heal" in name for name in names)
