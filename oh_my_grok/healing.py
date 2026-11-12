"""Failure-corpus projection (spec/HEALING.md).

Like the boulder file, a snapshot is derived and is never read back to decide a transition. ``collect`` reads jobs,
rejected reviews, and ``repair_cycle`` events. It does not read ``failure_snapshots``. The only write, besides the
snapshot rows, is one ``failures_collected`` audit event with a null job id, so no job is marked dirty and no boulder
is flushed.

``github_review_bot`` is reserved for a later slice and is not read.
"""
from __future__ import annotations

import json
import re
import uuid

from . import db, features
from .config import canonical_json, sha256_text
from .errors import GateRefused, NotFound, ValidationError

RESERVED_SOURCES = ("github_review_bot",)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _keep(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if v is not None}


def _obj(raw) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def _findings(raw) -> dict:
    try:
        val = json.loads(raw) if raw else []
    except ValueError:
        return {"findings_unparsed": raw}
    return {"findings": val}


def _identity(body: dict) -> str:
    if "event_id" in body:
        return str(body["event_id"])
    if "event_ids" in body:
        return ",".join(str(i) for i in body["event_ids"])
    if "review_id" in body:
        return str(body["review_id"])
    if "record" in body:
        return str(body["record"])
    return str(body["job_id"])


def _blocked(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, phase, blocked_from, failure_reason FROM jobs WHERE phase='blocked' ORDER BY id").fetchall()
    return [_keep({"class": "blocked", "source": "blocked_job", "job_id": r["id"], "phase": r["phase"],
                    "blocked_from": r["blocked_from"], "failure_reason": r["failure_reason"]}) for r in rows]


def _repairs(conn) -> tuple[list[dict], dict[str, list[int]]]:
    rows = conn.execute(
        "SELECT e.id, e.job_id, e.task_id, e.data_json FROM events e JOIN jobs j ON j.id=e.job_id "
        "WHERE e.kind='repair_cycle' ORDER BY e.id").fetchall()
    bodies, by_job, seen = [], {}, set()
    for r in rows:
        data = _obj(r["data_json"])
        bodies.append(_keep({
            "class": "repair", "source": "repair_cycle", "job_id": r["job_id"], "event_id": r["id"],
            "task_id": r["task_id"], "failures": data.get("failures"), "reason": data.get("reason"),
            "max": data.get("max")}))
        by_job.setdefault(r["job_id"], []).append(r["id"])
        seen.add(r["job_id"])
    # The counter is not cleared when a job later succeeds. If the log has no repair_cycle event, the counter
    # itself is the record.
    for r in conn.execute("SELECT id, repair_cycles FROM jobs WHERE repair_cycles>0 ORDER BY id"):
        if r["id"] in seen:
            continue
        bodies.append({"class": "repair", "source": "repair_cycle", "job_id": r["id"],
                        "failures": r["repair_cycles"], "record": "jobs.repair_cycles"})
    return bodies, by_job


def _repeated(by_job: dict[str, list[int]]) -> list[dict]:
    out = []
    for job_id, event_ids in by_job.items():
        if len(event_ids) < 2:
            continue
        ids = sorted(event_ids)
        out.append({"class": "repeated", "source": "repeated_failure", "job_id": job_id,
                     "count": len(ids), "event_ids": ids})
    return out


def _reviews(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, job_id, task_id, type, revision, plan_revision_id, findings_json, invalidated "
        "FROM reviews WHERE verdict='reject' ORDER BY id").fetchall()
    out = []
    for r in rows:
        body = _keep({"class": "review", "source": "review_finding", "job_id": r["job_id"], "review_id": r["id"],
                       "task_id": r["task_id"], "type": r["type"], "revision": r["revision"],
                       "plan_revision_id": r["plan_revision_id"], "invalidated": bool(r["invalidated"])})
        body.update(_findings(r["findings_json"]))
        out.append(body)
    return out


def _project(conn) -> list[dict]:
    repairs, by_job = _repairs(conn)
    bodies = [*_blocked(conn), *repairs, *_repeated(by_job), *_reviews(conn)]
    bodies.sort(key=lambda b: (b["class"], b["job_id"], b["source"], _identity(b)))
    items = [{"ordinal": n, "sha256": sha256_text(canonical_json(body)), "class": body["class"],
              "source": body["source"], "job_id": body["job_id"], "payload": body} for n, body in enumerate(bodies)]
    reserved = [i["source"] for i in items if i["source"] in RESERVED_SOURCES]
    if reserved:
        raise ValidationError("reserved healing source was collected", sources=sorted(set(reserved)))
    return items


def _corpus(items: list[dict]) -> str:
    return sha256_text(canonical_json([i["sha256"] for i in items]))


def collect(conn, mode: str) -> dict:
    """Write one snapshot. ``off`` raises and writes nothing. ``shadow`` and ``enforce`` both write; neither enqueues."""
    if mode == features.OFF:
        raise GateRefused("healing is off; collect writes nothing")
    if mode not in (features.SHADOW, features.ENFORCE):
        raise ValidationError("healing collect mode must be shadow or enforce", mode=mode)
    with db.tx(conn):
        items = _project(conn)
        corpus = _corpus(items)
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM failure_snapshots").fetchone()[0] + 1
        sid = str(uuid.uuid4())
        conn.execute("INSERT INTO failure_snapshots(id,seq,mode,item_count,corpus_sha256,created_at) "
                     "VALUES (?,?,?,?,?,?)", (sid, seq, mode, len(items), corpus, db.utcnow()))
        for item in items:
            conn.execute(
                "INSERT INTO failure_items(snapshot_id,ordinal,item_sha256,class,source,job_id,task_id,payload_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, item["ordinal"], item["sha256"], item["class"], item["source"], item["job_id"],
                 item["payload"].get("task_id"), canonical_json(item["payload"])))
        conn.execute("INSERT INTO events(ts,job_id,task_id,kind,actor,correlation_id,data_json) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (db.utcnow(), None, None, "failures_collected", "engine", sid,
                      canonical_json({"corpus_sha256": corpus, "item_count": len(items), "mode": mode,
                                      "snapshot_id": sid})))
    return {"written": True, "mode": mode, "snapshot_id": sid, "seq": seq, "sha256": corpus,
            "corpus_sha256": corpus, "item_count": len(items), "items": items}


def show(conn, snapshot_id: str | None = None) -> dict:
    """Read a snapshot. Does not write. Does not consult the flag: a snapshot already taken stays readable."""
    if snapshot_id is not None and not _UUID.match(snapshot_id):
        raise ValidationError("snapshot_id must be a UUID")
    if snapshot_id is None:
        row = conn.execute("SELECT * FROM failure_snapshots ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise NotFound("no failure snapshot")
    else:
        row = conn.execute("SELECT * FROM failure_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise NotFound("failure snapshot not found", snapshot_id=snapshot_id)
    items = []
    for r in conn.execute("SELECT ordinal, item_sha256, class, source, job_id, payload_json FROM failure_items "
                          "WHERE snapshot_id=? ORDER BY ordinal", (row["id"],)):
        items.append({"ordinal": r["ordinal"], "sha256": r["item_sha256"], "class": r["class"],
                      "source": r["source"], "job_id": r["job_id"], "payload": json.loads(r["payload_json"])})
    return {"snapshot_id": row["id"], "seq": row["seq"], "mode": row["mode"], "sha256": row["corpus_sha256"],
            "corpus_sha256": row["corpus_sha256"], "item_count": row["item_count"], "created_at": row["created_at"],
            "items": items}
