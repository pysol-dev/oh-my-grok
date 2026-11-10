"""Central allowed-transition tables. Every status write goes through check_*()."""
from __future__ import annotations

from .errors import Conflict

# ---- task statuses (spec §4 required set + awaiting_resume for Hephaestus without a resume routine)
QUEUED, DISPATCHED, AWAITING_RESUME, WORKING, SUBMITTED = "queued", "dispatched", "awaiting_resume", "working", "submitted"
PASSED, REJECTED, BLOCKED, FAILED, CANCELLED = "passed", "rejected", "blocked", "failed", "cancelled"

TASK_STATUSES = (QUEUED, DISPATCHED, AWAITING_RESUME, WORKING, SUBMITTED, PASSED, REJECTED, BLOCKED, FAILED, CANCELLED)
TASK_TERMINAL = frozenset({PASSED, REJECTED, FAILED, CANCELLED})
TASK_ACTIVE = frozenset({DISPATCHED, AWAITING_RESUME, WORKING, SUBMITTED})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({DISPATCHED, AWAITING_RESUME, BLOCKED, CANCELLED}),
    DISPATCHED: frozenset({WORKING, QUEUED, BLOCKED, CANCELLED}),
    AWAITING_RESUME: frozenset({WORKING, DISPATCHED, BLOCKED, CANCELLED}),
    WORKING: frozenset({SUBMITTED, QUEUED, BLOCKED, FAILED, CANCELLED}),
    SUBMITTED: frozenset({PASSED, REJECTED, BLOCKED, FAILED, CANCELLED}),
    BLOCKED: frozenset({QUEUED, CANCELLED, FAILED}),
    PASSED: frozenset(),
    REJECTED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

# ---- job phases (required outcomes + planning/research phases)
J_RESEARCH, J_ANALYSIS, J_PLANNING, J_PLAN_REVIEW = "research", "analysis", "planning", "plan_review"
J_AWAITING_APPROVAL, J_PLAN_APPROVED, J_RUNNING = "awaiting_approval", "plan_approved", "running"
J_BLOCKED, J_FAILED, J_CANCELLED, J_DONE = "blocked", "failed", "cancelled", "done"

JOB_PHASES = (J_RESEARCH, J_ANALYSIS, J_PLANNING, J_PLAN_REVIEW, J_AWAITING_APPROVAL, J_PLAN_APPROVED,
              J_RUNNING, J_BLOCKED, J_FAILED, J_CANCELLED, J_DONE)
JOB_TERMINAL = frozenset({J_DONE, J_FAILED, J_CANCELLED})
_STOP = frozenset({J_BLOCKED, J_FAILED, J_CANCELLED})

_REPLAN_ENTRY = frozenset({J_RESEARCH, J_ANALYSIS})  # escalation sends a job back to its earliest incomplete phase

JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    J_RESEARCH: frozenset({J_ANALYSIS}) | _STOP,
    J_ANALYSIS: frozenset({J_PLANNING, J_RESEARCH, J_DONE}) | _STOP,  # J_DONE only for read-only investigations
    J_PLANNING: frozenset({J_PLAN_REVIEW}) | _REPLAN_ENTRY | _STOP,
    J_PLAN_REVIEW: frozenset({J_PLANNING, J_AWAITING_APPROVAL}) | _REPLAN_ENTRY | _STOP,
    J_AWAITING_APPROVAL: frozenset({J_RUNNING, J_PLAN_APPROVED, J_PLANNING}) | _REPLAN_ENTRY | _STOP,
    J_PLAN_APPROVED: frozenset({J_RUNNING, J_PLANNING}) | _REPLAN_ENTRY | _STOP,
    J_RUNNING: frozenset({J_DONE}) | _REPLAN_ENTRY | _STOP,
    # resume from blocked goes back to the phase recorded in jobs.blocked_from
    J_BLOCKED: frozenset({J_RESEARCH, J_ANALYSIS, J_PLANNING, J_PLAN_REVIEW, J_AWAITING_APPROVAL, J_PLAN_APPROVED, J_RUNNING,
                          J_FAILED, J_CANCELLED}),
    J_DONE: frozenset(),
    J_FAILED: frozenset(),
    J_CANCELLED: frozenset(),
}

# ---- dispatch outbox statuses
D_PENDING, D_SENDING, D_ACCEPTED, D_RETRY_WAIT, D_FAILED = "pending", "sending", "accepted", "retry_wait", "failed"
DISPATCH_STATUSES = (D_PENDING, D_SENDING, D_ACCEPTED, D_RETRY_WAIT, D_FAILED)
DISPATCH_TRANSITIONS = {
    D_PENDING: frozenset({D_SENDING, D_FAILED}),
    D_SENDING: frozenset({D_ACCEPTED, D_RETRY_WAIT, D_FAILED}),
    D_RETRY_WAIT: frozenset({D_SENDING, D_FAILED}),
    D_ACCEPTED: frozenset(),
    D_FAILED: frozenset(),
}

# ---- roles
ROLES = ("hephaestus", "explorer", "librarian", "metis", "momus", "executor", "code-reviewer", "qa-executor")
READ_ONLY_ROLES = frozenset({"explorer", "librarian", "metis", "momus", "code-reviewer", "qa-executor"})

# task kinds -> role
KIND_ROLE = {
    "explore": "explorer",
    "research": "librarian",
    "analyze": "metis",
    "plan": "hephaestus",
    "plan_review": "momus",
    "implement": "executor",
    "code_review": "code-reviewer",
    "qa": "qa-executor",
    "integrate": "hephaestus",
}


def check_task(src: str, dst: str) -> None:
    if dst not in TASK_TRANSITIONS.get(src, frozenset()):
        raise Conflict(f"illegal task transition {src} -> {dst}", src=src, dst=dst)


def check_job(src: str, dst: str) -> None:
    if dst not in JOB_TRANSITIONS.get(src, frozenset()):
        raise Conflict(f"illegal job transition {src} -> {dst}", src=src, dst=dst)


def check_dispatch(src: str, dst: str) -> None:
    if dst not in DISPATCH_TRANSITIONS.get(src, frozenset()):
        raise Conflict(f"illegal dispatch transition {src} -> {dst}", src=src, dst=dst)
