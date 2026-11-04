# omo-grok architecture

omo-grok is a durable workflow bus that coordinates eight logical roles across existing Grok Bots. It runs on the
shared Bot computer. The parts:

- One SQLite database (`state/omo.sqlite3`).
- One shared engine (`omo_grok/engine.py`), used by both front ends:
  - the stdio MCP server `omo` (`omo_grok/mcp_server.py`)
  - the operator CLI `omoctl` (`omo_grok/cli.py`)
- A transactional outbox that wakes role routines over HTTP webhooks.

```
user ─chat─▶ Hephaestus ─MCP─▶ omo engine ◀─MCP── role routines (Explorer, Librarian, Metis, Momus,
                                   │  ▲                     Executor, Code Reviewer, QA)
                          outbox ──┘  └── claim/complete with exact ids
                             │ POST {schema_version, job_id, task_id, dispatch_id, attempt} + Bearer key
                             ▼
                    omo-bus-<role> webhook routine on the bound Bot
```

## Modules
| Module | Purpose |
|---|---|
| `db.py` | Connection (WAL, foreign_keys, busy_timeout), `BEGIN IMMEDIATE` transactions, versioned checksummed migrations, backup/restore with integrity checks |
| `states.py` | Explicit task, job and dispatch transition tables. Illegal transitions raise |
| `engine.py` | Every workflow rule: submit, the DAG, claim/lease, complete handlers, plan revisions, approvals, verification, repair loop, independence, final gate, reconcile, cancel/resume, status/export |
| `mcp_server.py` | The 8 MCP tools (pydantic-validated). Logs go to stderr, redacted. No approve/ship/shell/secret tools |
| `cli.py` | `omoctl`: doctor, migrate, status, submit, plan show, approve/deny (TTY), cancel (TTY), resume, reconcile, export, backup/restore, hook set/check/list, roles, test |
| `transport.py` | Bounded-timeout HTTP POST (stdlib). Hook URLs must be https (`hooks.py`); plain http is accepted only for 127.0.0.1/localhost with `OMO_ALLOW_INSECURE_HOOKS=1` (tests) |
| `hooks.py` | Webhook URL and key store `state/secrets/hooks.json` (0600 file in a 0700 dir). Separate from job state |
| `artifacts.py` | Immutable, content-addressed artifact files. Path-traversal and symlink checks. sha256 verified on read |
| `schemas.py` | Strict per-kind result schemas and the `blocked` / `not_applicable` alternatives |
| `verifier.py` | Deterministic verifier profiles (argv, no shell, isolated `-I` interpreter, env allowlist, timeout, protected + editable paths, pre-run rejection of shadowing modules and interpreter/test hooks). Template, missing or non-isolated profiles fail closed |
| `gitops.py` | Per-job worktree and branch (`omo/<job-id-prefix>`). Repo hooks are disabled. No reset, clean, force or push |
| `fixture.py` | Synthetic `calc` fixture repo: a failing test, a real fix, and a fake "fix" that edits tests or prints PASS |
| `redact.py` | Removes bearer tokens, Authorization values, key/token/secret assignments, common token prefixes, PEM private keys and registered secrets |
| `live.py` | Harness for live items 20–22 (`omoctl test live --fixture-only`). Reports NOT RUN without real bindings |

## Data model (migration 1 plus migration 2)
The tables:

- `jobs`, `tasks`, `task_deps`, `task_attempts`
- `artifacts`, `task_inputs`
- `plan_revisions`, `approvals`
- `role_bindings` (versioned)
- `dispatch_outbox`, `verifications`, `reviews`, `gate_results`
- `events` (append-only audit)
- `hooks_meta` (booleans only)
- `schema_migrations` (version and checksum)

All writes use short `BEGIN IMMEDIATE` transactions with compare-and-set on the expected state. No transaction is
held open during HTTP or verifier runs.

## Lifecycle
1. `submit` builds the per-job DAG.
2. **Research.** `explore` and `research` run in parallel.
3. **Analysis.** Once both pass, the engine creates `analyze` with both artifacts pinned as inputs (with sha256).
4. **Planning.** `plan` is a Hephaestus task. The plan artifact becomes an immutable `plan_revision` with a
   canonical hash.
5. **Plan review.** One `plan_review` task (Momus) is created per revision. The result must echo the exact hash.
   A reject or a deny creates a new revision. The number of revision rounds is capped.
6. **Approval.** The job waits in `awaiting_approval` until `omoctl approve JOB --plan-hash H` is run at a TTY,
   which records a SOFT receipt. The receipt is bound to the job, the hash, the requirements hash and the scope.
   A plan change revokes it. Fixture jobs may instead use an explicit *synthetic* approval, labelled as such.
7. **Implementation.** On approval the engine creates a worktree and branch from the base revision, then queues
   `implement`.
8. **Verification.** After the Executor reports a commit, the engine checks that the commit is on the job branch
   and descends from the base. It then runs the verifier profile against that exact revision.
9. **Repair loop.** If verification fails, a new `implement` cycle starts, with prior failures passed in the
   packet. After `max_repair_cycles` (3) the job is `blocked`.
10. **Review.** When verification passes, `code_review` and `qa` start in parallel. Both are bound to the
    candidate revision. A reject starts a repair cycle.
11. **Integration.** `integrate` is a Hephaestus task, with decision `finalize` or `repair`.
12. **Final gate.** This is deterministic code. It checks:
    - the approval is valid
    - the verifier is re-run on the exact final revision and passes
    - reviews of that revision are accepted
    - artifacts pass integrity checks
    - the worktree is clean
    - the independence rules hold

    If all pass, the job is `done` with an assurance label.

## Dispatch
- The engine publishes an outbox row in the same transaction that makes a task ready. The HTTP POST happens after
  commit. Dispatch requires a *verified* binding with a routine id and a configured hook.
- Per-Bot serialization: `max_concurrent`, counted across roles that share a Bot.
- An hourly per-Bot cap.
- 200 means accepted. It does not mean claimed.
- 401, 403, 404 and 410 are permanent: the task is blocked with a diagnostic.
- 429, 5xx and network errors are retried with persisted, capped exponential backoff, honouring Retry-After.
- `reconcile`:
  - expires leases
  - turns stale `sending` rows into uncertain retries
  - re-wakes accepted-but-unclaimed tasks, bounded by `max_rewakes`
  - re-runs interrupted verification
  - schedules ready work and flushes the outbox
- A claim supersedes pending duplicates. Only the current dispatch id and attempt id are accepted.
- Hephaestus tasks with no verified resume routine get a local dispatch row and wait as `awaiting_resume` for the
  next Hephaestus chat turn. There is never a self-webhook.

## Independence and assurance
The engine compares the Executor attempt's Bot and independence group with each reviewer's. The labels are:

- `separate_bots_same_account`: the Bots are distinct, but they share one account and computer.
- `reduced_non_independent`

Each label gets a `+soft_approval` or `+synthetic_approval` suffix. Real jobs with reduced assurance fail the gate
unless the approval scope includes `accept_reduced_assurance`.

## Policy (`config/policy.json`, optional; unknown keys rejected)
| Key | Default |
|---|---|
| `max_repair_cycles` | 3 |
| `max_plan_rounds` | 3 |
| `max_attempts_per_task` | 3 |
| `lease_seconds` | 1800 |
| `rewake_after_seconds` | 900 |
| `max_rewakes` | 3 |
| `dispatch_max_retries` | 5 |
| `backoff_base_seconds` | 30 |
| `backoff_cap_seconds` | 1800 |
| HTTP connect / read timeouts | 5 s / 15 s |
| `per_bot_hourly_dispatch_limit` | 20 |
| `max_sends_per_flush` | 10 |
| `repo_allowlist` | [] (any git repo) |
| `require_independent_assurance` | true |
| `fixture_allow_reduced_assurance` | true |
