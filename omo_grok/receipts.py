"""Engine-side tool receipts (spec/ADAPTIVE.md section 8).

A receipt is written only by engine code that itself ran, re-ran or delivered something. Levels:
  engine_executed    the engine ran the tool and holds its output
  engine_reverified  the engine independently re-ran/re-checked what a bot claimed
  engine_delivered   the engine put material in a packet; this proves delivery, never that it was read or obeyed
Only the first two may satisfy a gate. Nothing a bot reports is ever stored at these levels."""
from __future__ import annotations

import json
import uuid

from . import db

GATE_LEVELS = ("engine_executed", "engine_reverified")
LEVELS = ("engine_executed", "engine_reverified", "engine_delivered")


def record(conn, *, job_id: str, kind: str, level: str, status: str, tool: str, task_id: str | None = None,
           revision: str | None = None, tool_version: str | None = None, argv_sha256: str | None = None,
           config_sha256: str | None = None, input_sha256: str | None = None, output_sha256: str | None = None,
           artifact_ids: list[str] | None = None, summary: dict | None = None) -> str:
    if level not in LEVELS:
        raise ValueError(f"unknown receipt level {level!r}")
    rid = str(uuid.uuid4())
    conn.execute("INSERT INTO tool_receipts(id,job_id,task_id,kind,level,status,revision,tool,tool_version,argv_sha256,"
                 "config_sha256,input_sha256,output_sha256,artifact_ids_json,summary_json,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, job_id, task_id, kind, level, status, revision, tool, tool_version, argv_sha256, config_sha256,
                  input_sha256, output_sha256, json.dumps(artifact_ids or []),
                  json.dumps(summary or {}, sort_keys=True), db.utcnow()))
    return rid


def latest(conn, job_id: str, kind: str, revision: str | None = None, levels=GATE_LEVELS):
    q = ",".join("?" * len(levels))
    args = [job_id, kind, *levels]
    rev = ""
    if revision is not None:
        rev, args = " AND revision=?", [*args, revision]
    row = conn.execute(f"SELECT * FROM tool_receipts WHERE job_id=? AND kind=? AND level IN ({q}){rev} "
                       f"ORDER BY created_at DESC, rowid DESC LIMIT 1", args).fetchone()
    return row


def listing(conn, job_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM tool_receipts WHERE job_id=? ORDER BY created_at, rowid", (job_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["artifact_ids"] = json.loads(d.pop("artifact_ids_json") or "[]")
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        out.append(d)
    return out
