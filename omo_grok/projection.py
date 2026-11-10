"""Write-only boulder/active-plan projection (spec/ADAPTIVE.md section 6).

The files under ``jobs/<job_id>/.omo/`` are a pure function of the SQLite database. Nothing in this package ever reads
them to decide anything: the only thing read back is the file's own hash and generation, to detect tampering and to
avoid regressing. The database stays the sole transition authority.

Writer protocol (one writer function, everything else calls it):
  1. exclusive ``flock`` on ``boulder.lock`` with a bounded wait; a timeout marks the projection stale and returns,
  2. inside the lock: tamper check, then a read snapshot of the job; ``generation = MAX(events.id)`` of that snapshot,
  3. render deterministically; skip when an already-trusted file is newer or identical,
  4. record the intended hashes (pending), write temp files, ``fsync``, ``os.replace``, ``fsync`` the directory,
  5. record the committed hashes.
No database write transaction is open during file I/O and the lock order is always file lock first, database second."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

from . import artifacts as art
from . import db
from .errors import OmoError
from .paths import Paths

SCHEMA_VERSION = 2
AGENT = "omo-grok"
TERMINAL = ("done", "failed", "cancelled")
_FAULT_ENV = "OMO_FAULT_INJECT"  # test-only: die abruptly (like kill -9) at a named point of the writer


def _fault(point: str) -> None:
    if os.environ.get(_FAULT_ENV) == point:
        os._exit(137)


def omo_dir(paths: Paths, job_id: str) -> Path:
    return paths.job_dir(job_id) / ".omo"


def plan_name(job_id: str) -> str:
    return f"omo-{job_id[:8]}"


def work_status(phase: str) -> str:
    return {"blocked": "paused", "done": "completed", "cancelled": "abandoned", "failed": "abandoned"}.get(phase, "active")


def session_status(attempt_status: str | None) -> str:
    return {"working": "running", "completed": "completed"}.get(attempt_status or "", "cancelled")


def next_action(job, tasks) -> str:
    ph = job["phase"]
    if ph == "awaiting_approval":
        return "user: omoctl plan show {0} then omoctl approve {0} --plan-hash <hash> (or deny)".format(job["id"])
    if ph == "blocked":
        return f"inspect blocked reason, fix the cause, then omoctl resume {job['id']}"
    if ph in TERMINAL:
        return "none (terminal)"
    if any(t["status"] == "awaiting_resume" for t in tasks):
        return "hephaestus: claim the awaiting_resume task in-chat (no resume routine bound)"
    deferred = [t["block_reason"] for t in tasks if t["status"] == "queued" and t["block_reason"]]
    if deferred:
        return "dispatch deferred: " + deferred[0]
    return "waiting for agents / run omoctl reconcile"


def task_key(t) -> str:
    return f"{t['kind']}-c{t['cycle']}-{t['id'][:8]}"


# --------------------------------------------------------------------------------------------- snapshot + render
def snapshot(conn, job_id: str) -> dict | None:
    """One consistent read of everything the projection shows."""
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return None
        snap = {"job": job, "generation": conn.execute("SELECT MAX(id) m FROM events WHERE job_id=?",
                                                       (job_id,)).fetchone()["m"] or 0}
        snap["tasks"] = conn.execute("SELECT * FROM tasks WHERE job_id=? ORDER BY created_at, rowid", (job_id,)).fetchall()
        snap["sessions"] = conn.execute(
            "SELECT s.*, a.status AS attempt_status, a.ended_at AS ended_at FROM work_sessions s "
            "LEFT JOIN task_attempts a ON a.id=s.attempt_id WHERE s.job_id=? ORDER BY s.started_at, s.rowid",
            (job_id,)).fetchall()
        rev = conn.execute("SELECT p.*, a.rel_path, a.sha256 AS art_sha FROM plan_revisions p JOIN artifacts a ON "
                           "a.id=p.artifact_id WHERE p.id=?", (job["current_plan_revision_id"],)).fetchone() \
            if job["current_plan_revision_id"] else None
        snap["plan"] = rev
        snap["approval"] = conn.execute("SELECT mode FROM approvals WHERE job_id=? AND revoked_at IS NULL "
                                        "ORDER BY created_at DESC LIMIT 1", (job_id,)).fetchone()
        sha = job["candidate_revision"]
        snap["verified"] = bool(sha and conn.execute(
            "SELECT 1 FROM verifications WHERE job_id=? AND revision=? AND purpose='candidate' AND status='passed'",
            (job_id, sha)).fetchone())
        snap["reviews"] = {typ: bool(sha and conn.execute(
            "SELECT 1 FROM reviews WHERE job_id=? AND type=? AND revision=? AND invalidated=0 AND verdict='accept'",
            (job_id, typ, sha)).fetchone()) for typ in ("code", "qa")}
        g = conn.execute("SELECT passed FROM gate_results WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                         (job_id,)).fetchone()
        snap["gate_passed"] = bool(g and g["passed"])
        return snap
    finally:
        if owns:
            conn.execute("COMMIT")


def _fence(text: str) -> str:
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_plan_md(snap: dict, plan_text: str | None, plan_note: str | None) -> str:
    job, tasks = snap["job"], snap["tasks"]
    L = [f"# {plan_name(job['id'])}", "",
         f"Job: {job['id']}", f"Goal: {' '.join(job['goal'].split())[:300]}",
         f"Intent: {job['intent'] or 'unspecified'}   Class: {job['roster_class'] or 'legacy'}   Phase: {job['phase']}",
         "", "Generated from the omo-grok database. Editing this file changes nothing; the engine never reads it.", "",
         "## TODOs", ""]
    for i, t in enumerate(tasks, 1):
        box = {"passed": "x", "blocked": "~", "rejected": "~", "cancelled": "~"}.get(t["status"], " ")
        extra = f" ({t['status']})" if t["status"] in ("rejected", "cancelled", "blocked") else ""
        L.append(f"- [{box}] {i}. {t['kind']} ({t['role']}, cycle {t['cycle']}){extra}")
    L += ["", "## Final Verification Wave", ""]
    L.append(f"- [{'x' if snap['verified'] else ' '}] F1. Deterministic verifier passed on the exact candidate revision")
    L.append(f"- [{'x' if snap['reviews']['code'] else ' '}] F2. Independent code review accepted the exact revision")
    L.append(f"- [{'x' if snap['reviews']['qa'] else ' '}] F3. Independent QA accepted the exact revision")
    L.append(f"- [{'x' if snap['gate_passed'] else ' '}] F4. Final gate passed")
    plan = snap["plan"]
    if plan:
        L += ["", f"## Plan (hash {plan['content_hash']})", ""]
        if plan_text is not None:
            f = _fence(plan_text)
            L += [f"{f}markdown", plan_text.rstrip("\n"), f]
        else:
            L.append(f"Plan text omitted: {plan_note or 'artifact integrity check failed'}.")
    return "\n".join(L) + "\n"


def render_boulder(snap: dict, paths: Paths, plan_md_path: Path) -> dict:
    job = snap["job"]
    jid = job["id"]
    status = work_status(job["phase"])
    sessions = snap["sessions"]
    by_task: dict[str, list] = {}
    for s in sessions:
        by_task.setdefault(s["task_id"], []).append(s)
    tasks_by_id = {t["id"]: t for t in snap["tasks"]}
    task_sessions = {}
    for tid, ss in by_task.items():
        t = tasks_by_id.get(tid)
        if t is None:
            continue
        last = ss[-1]
        st = session_status(last["attempt_status"])
        entry = {"task_key": task_key(t), "task_label": t["kind"], "task_title": f"{t['kind']} ({t['role']})",
                 "session_id": last["session_token"], "agent": t["role"], "category": t["role"],
                 "started_at": last["started_at"], "status": st, "updated_at": t["updated_at"]}
        if st != "running" and last["ended_at"]:
            entry["ended_at"] = last["ended_at"]
        task_sessions[task_key(t)] = entry
    tokens = [s["session_token"] for s in sessions]
    work = {"work_id": jid, "active_plan": str(plan_md_path), "plan_name": plan_name(jid), "status": status,
            "started_at": job["created_at"], "updated_at": job["updated_at"], "session_ids": tokens,
            "session_origins": {tok: "omo-grok-claim" for tok in tokens}, "agent": AGENT,
            "worktree_path": job["worktree_path"], "task_sessions": task_sessions}
    if work["worktree_path"] is None:
        del work["worktree_path"]
    if status in ("completed", "abandoned"):
        work["ended_at"] = job["updated_at"]
    plan = snap["plan"]
    out = {"schema_version": SCHEMA_VERSION,
           "active_work_id": jid if status in ("active", "paused") else None,
           "works": {jid: work},
           "active_plan": work["active_plan"], "started_at": work["started_at"], "status": status,
           "session_ids": tokens, "plan_name": work["plan_name"], "agent": AGENT,
           "task_sessions": task_sessions,
           "x_omo_grok": {"job_id": jid, "generation": snap["generation"], "projection": "derived-from-sqlite",
                          "resume": {"tool": "omo.status", "job_id": jid}, "roster_class": job["roster_class"],
                          "intent": job["intent"], "phase": job["phase"],
                          "plan_hash": plan["content_hash"] if plan else None,
                          "candidate_revision": job["candidate_revision"],
                          "next_action": next_action(job, snap["tasks"])}}
    if work.get("worktree_path"):
        out["worktree_path"] = work["worktree_path"]
    return out


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode()


# --------------------------------------------------------------------------------------------- file helpers
def _sha(b: bytes | None) -> str | None:
    return hashlib.sha256(b).hexdigest() if b is not None else None


def _unlink_if_symlink(p: Path) -> bool:
    """Remove a symlink without following it. A regular file is left untouched."""
    try:
        mode = os.lstat(p).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISLNK(mode):
        return False
    os.unlink(p)
    return True


def _read(p: Path) -> bytes | None:
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:  # a symlink; the caller replaces it and does not read the target
            return None
        raise
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(target: Path, data: bytes, point: str | None = None) -> None:
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if point:
            _fault(point)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)


def _remove_stale_temps(*dirs: Path) -> None:
    for d in dirs:
        if d.is_dir():
            for p in d.glob(".*.tmp"):  # we hold the exclusive lock, so any temp file belongs to a dead writer
                try:
                    p.unlink()
                except OSError:
                    pass


def _record(conn, job_id: str, status: str, error: str | None = None, **cols) -> None:
    now = db.utcnow()
    keys = ["generation", "sha256", "pending_sha256", "plan_sha256", "pending_plan_sha256"]
    given = {k: cols[k] for k in keys if k in cols}
    with db.tx(conn):
        row = conn.execute("SELECT * FROM boulder_projection WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            vals = {"generation": 0, "sha256": None, "pending_sha256": None, "plan_sha256": None,
                    "pending_plan_sha256": None, **given}
            conn.execute("INSERT INTO boulder_projection(job_id,generation,sha256,pending_sha256,plan_sha256,"
                         "pending_plan_sha256,status,last_error,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (job_id, vals["generation"], vals["sha256"], vals["pending_sha256"], vals["plan_sha256"],
                          vals["pending_plan_sha256"], status, error, now))
        else:
            sets = "".join(f", {k}=?" for k in given)
            conn.execute(f"UPDATE boulder_projection SET status=?, last_error=?, updated_at=?{sets} WHERE job_id=?",
                         (status, error, now, *given.values(), job_id))


def _event(conn, job_id: str, kind: str, **data) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) VALUES (?,?,?,?,?,?,?)",
                     (db.utcnow(), job_id, None, kind, "projection", None, json.dumps(data, sort_keys=True)))


def _lock(path: Path, seconds: float):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)


# --------------------------------------------------------------------------------------------- the writer
def project(conn, paths: Paths, job_id: str, lock_seconds: float = 10.0) -> dict:
    """Bring ``jobs/<job_id>/.omo`` in line with the database. Never raises: a projection problem is recorded as
    ``stale`` and must never stop, fail or change a job."""
    prior = None
    try:
        prior = conn.execute("SELECT status FROM boulder_projection WHERE job_id=?", (job_id,)).fetchone()
        return _project(conn, paths, job_id, lock_seconds)
    except Exception as e:  # noqa: BLE001 - deliberate: the projection can never break the engine
        msg = f"{type(e).__name__}: {e}"[:300]
        try:
            _record(conn, job_id, "stale", msg)
            if not prior or prior["status"] != "stale":
                _event(conn, job_id, "projection_stale", error=msg)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "stale", "tamper": False, "error": msg}


def _project(conn, paths: Paths, job_id: str, lock_seconds: float) -> dict:
    d = omo_dir(paths, job_id)
    plans = d / "plans"
    plans.mkdir(parents=True, exist_ok=True, mode=0o700)
    gi = d / ".gitignore"
    if not gi.exists():
        _write_atomic(gi, b"*\n")
    boulder_path = d / "boulder.json"
    md_path = plans / f"{plan_name(job_id)}.md"
    lock_path = d / "boulder.lock"
    symlink_replaced = _unlink_if_symlink(lock_path)
    fd = _lock(lock_path, lock_seconds)
    if fd is None:
        prior = conn.execute("SELECT status FROM boulder_projection WHERE job_id=?", (job_id,)).fetchone()
        _record(conn, job_id, "stale", "timed out waiting for boulder.lock")
        if not prior or prior["status"] != "stale":
            _event(conn, job_id, "projection_stale", error="timed out waiting for boulder.lock")
        return {"status": "stale", "tamper": False, "error": "lock timeout"}
    try:
        _fault("after_lock")
        _remove_stale_temps(d, plans)
        if _unlink_if_symlink(boulder_path):
            symlink_replaced = True
        if _unlink_if_symlink(md_path):
            symlink_replaced = True
        rec = conn.execute("SELECT * FROM boulder_projection WHERE job_id=?", (job_id,)).fetchone()
        cur_b, cur_md = _read(boulder_path), _read(md_path)
        trusted_b = {rec["sha256"], rec["pending_sha256"]} - {None} if rec else set()
        trusted_md = {rec["plan_sha256"], rec["pending_plan_sha256"]} - {None} if rec else set()
        if cur_b is None:
            tamper = bool(rec and rec["sha256"])
        else:
            tamper = _sha(cur_b) not in trusted_b
        if not tamper and cur_md is not None and trusted_md:
            tamper = _sha(cur_md) not in trusted_md
        elif not tamper and cur_md is None and rec and rec["plan_sha256"]:
            tamper = True
        if symlink_replaced:
            tamper = True  # a symlink is not read; it is removed and the projection is rewritten
        if tamper:
            _event(conn, job_id, "boulder_tamper", boulder_present=cur_b is not None, plan_present=cur_md is not None,
                   action="regenerated from the database; file contents were not imported")
        snap = snapshot(conn, job_id)
        if snap is None:
            return {"status": "missing_job", "tamper": False}
        _fault("after_snapshot")
        plan_text = plan_note = None
        if snap["plan"]:
            try:
                plan_text = art.read_text(paths.artifacts_dir(job_id), snap["plan"]["rel_path"], snap["plan"]["art_sha"])
            except OmoError as e:
                plan_note = f"artifact integrity check failed ({e.message})"
        new_md = render_plan_md(snap, plan_text, plan_note).encode()
        new_b = encode(render_boulder(snap, paths, md_path))
        gen = snap["generation"]
        if not tamper and cur_b is not None:
            try:
                existing_gen = json.loads(cur_b)["x_omo_grok"]["generation"]
            except (ValueError, KeyError, TypeError):
                existing_gen = -1
            if isinstance(existing_gen, int) and existing_gen > gen:
                return {"status": "skipped_newer", "tamper": False, "generation": existing_gen,
                        "path": str(boulder_path)}
        if not tamper and cur_b == new_b and cur_md == new_md:
            if rec is None or rec["status"] != "ok" or rec["generation"] != gen:
                _record(conn, job_id, "ok", None, generation=gen, sha256=_sha(new_b), plan_sha256=_sha(new_md),
                        pending_sha256=None, pending_plan_sha256=None)
            return {"status": "unchanged", "tamper": False, "generation": gen, "path": str(boulder_path)}
        _record(conn, job_id, "writing", None, pending_sha256=_sha(new_b), pending_plan_sha256=_sha(new_md))
        if cur_md != new_md or tamper:
            _write_atomic(md_path, new_md)
        if cur_b != new_b or tamper:
            _write_atomic(boulder_path, new_b, point="after_temp_write")
        _fault("after_replace")
        _record(conn, job_id, "ok", None, generation=gen, sha256=_sha(new_b), plan_sha256=_sha(new_md),
                pending_sha256=None, pending_plan_sha256=None)
        _fault("after_db_record")
        return {"status": "written", "tamper": tamper, "generation": gen, "path": str(boulder_path)}
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
