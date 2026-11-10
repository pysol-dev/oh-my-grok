# ADAPTIVE ORCHESTRATION SPEC (sibling to `spec/IMPLEMENTATION.md`)

Status of this document: **DRAFT FOR IMPLEMENTATION, version 1 (2026-11-06)**. It records the corrected design ("v2")
that follows the read-only audit of `acb40e5`. It describes what the code in this branch is required to do and what it
is forbidden to claim. It is not evidence that anything is installed or operational.

`spec/IMPLEMENTATION.md` (the original spec) is **not edited**. Its SHA-256 is
`9643ee06f6817b08c57ea585fd8f919d4b23639bffd817a4a3084c7c9d0d30ae` (see `spec/IMPLEMENTATION.md.sha256`); a test
(`tests/unit/test_spec_integrity.py`) fails if the bytes change. Where this document differs from the original, the
difference is listed in section 1 and nowhere else.

**Operational state while this document is in force: NOT OPERATIONAL.** Implementing a feature is not installing it.
`omoctl doctor` derives its verdict from recorded evidence (section 12), never from a constant, and the original
section 13 stop conditions (real cross-Bot webhook/MCP run on the host) still apply in full.

## 1. Relationship to the original spec and register of supersessions

The original eight logical roles, the SQLite engine as the only transition authority, the human-only approval, the
exact-revision gate, the same-account trust limits and the "never claim OPERATIONAL without a live cross-Bot test"
rule all remain in force. Every deviation is one of: **I** (interpretation the original text allows),
**E** (extension that adds capability and removes nothing), **S** (supersession of original wording).

| ID | Original reference | Kind | What this spec does |
|---|---|---|---|
| SUP-1 | s3 "initial per-job DAG should be" (explore, research, metis, plan...) | I | The full DAG becomes the `heavy` class. `quick` and `standard` create a strict subset of tasks. s3 itself says "optimize by using fewer subtasks ... do not silently remove required approval or verification gates"; every class keeps plan, Momus plan review, user approval, executor, deterministic verifier, independent code review, QA, integrate, and the final gate. |
| SUP-2 | s3 "external research ... may be marked `not_applicable` only with an explicit reason and record" | I | In `quick`, and in `standard` without `external_api`, the research task is **absent** instead of present-and-not_applicable. The record the original requires is a persisted, validated scope attestation (`external_api=false`) plus an event. A diff or plan that contradicts the attestation escalates (section 5.4). A present research task keeps the original `not_applicable` rule unchanged. |
| SUP-3 | s5 "Implement and document these functions" (8 tools) | E | Adds two MCP tools: `escalate` (can only add work) and `docs` (engine-brokered Context7 lookup). Adds optional arguments `intent`, `roster_class`, `scope` to `submit`. None of the forbidden tool types (`approve_plan`, `ship`, `set_hook`, `override_verification`, `mark_done`, `execute_shell`, secret retrieval) is added. |
| SUP-4 | s4 "Required job outcomes ... `done`" | E | A job with `intent=investigation` may reach `done` from the `analysis` phase with a read-only investigation result. It never has a plan, approval, worktree, executor task, or candidate revision. It is the only path to `done` that skips the final gate, and it is allowed only because no code can change (section 4.2, enforced in four places). |
| SUP-5 | s9 "Never call `npx tsc --noEmit` an LSP verification for every language" | I (strengthens) | Diagnostics gating is performed by an engine-side Language Server Protocol client talking JSON-RPC to a real language server. A command-line checker run is never labelled LSP. |
| SUP-6 | s1 "minimal dependencies only as justified"; s0 "do not solicit blanket permission to install packages" | E | Language servers and Node are installed only by `scripts/bootstrap.sh`, from hash-pinned lock files, into `tools/` (git-ignored), never into the runtime `.venv`. `bootstrap.sh` installs nothing else and never uses sudo. |
| SUP-7 | s0 "Context7 when available" | E | The engine contains a Context7 MCP client (`omo_grok/context7.py`). Bots keep their own Context7 access (whose use is unobservable and only ever an `agent_claimed` fact). |
| SUP-8 | s2 repository layout "illustrative"; every path `/workspace/omo` | I | No code, test, or shipped config may contain `/workspace/omo`. Paths derive from the code root (`${OMO_CODE_ROOT}`), the interpreter (`${OMO_PYTHON}`), and the tools directory (`${OMO_TOOLS}`). The documented host location remains `/workspace/omo`, but nothing depends on it. |
| SUP-9 | s6 CLI list | E | Adds `omoctl lsp`, `omoctl boulder`, `omoctl features`, `omoctl mcp-command`, `omoctl docs` and `omoctl receipts`. No existing command changes meaning. |

Nothing in section 1 weakens: approval, Momus review of the exact plan hash, the revision-bound final gate, reviewer
independence, fail-closed verifier behaviour, or secret handling.

## 2. Verified facts (checked by running or reading, with the source)

Each row says how it was verified. Anything not in this table is an assumption and is listed in section 13.

| Fact | Evidence |
|---|---|
| Upstream `oh-my-openagent` boulder types at pin `9afd6e6c307b84a750f2986b69377b050a46c05f`: `BoulderState` (`schema_version?: 2`, `active_work_id`, `works`, mirrored `active_plan`, `started_at`, `status`, `session_ids`, `plan_name`, `agent`, `worktree_path`, `task_sessions`), `BoulderWorkStatus = active\|completed\|paused\|abandoned`, `TaskSessionState.status = running\|completed\|cancelled`, file `.omo/boulder.json`, plans in `.omo/plans`, checklist grammar `## TODOs` / `- [ ] 1. Title` / `## Final Verification Wave` / `- [ ] F1. ...`, `[~]` = blocked | `gh api .../packages/boulder-state/src/types.ts?ref=9afd6e6...`, `plan-checklist.ts`, `storage/constants.ts` |
| Upstream writer is **not** atomic or locked: `writeBoulderState` is `mkdir` + `writeFileSync(JSON.stringify(...))` | `storage/write-state.ts` at the same pin |
| `ruff server` 0.17.4 advertises `diagnosticProvider` and answers `textDocument/diagnostic` (pull) with `items` | manual JSON-RPC probe during design; re-proved by `tests/integration/test_lsp_real_servers.py` |
| `basedpyright-langserver --stdio` 1.40.2 does **not** advertise `diagnosticProvider`; it pushes `textDocument/publishDiagnostics` carrying the `version` sent in `didOpen`; it issues server-to-client `workspace/configuration` requests | manual probe; v2's assumption "pull may work" was wrong, so the client implements both modes |
| `typescript-language-server` 5.3.0 with `typescript` 5.9.3 pushes diagnostics, advertises no `diagnosticProvider`, needs `initializationOptions.tsserver.path`, and needs a `node` on `PATH` | manual probe; re-proved by the real-server tests |
| The Context7 remote MCP server `https://mcp.context7.com/mcp` answers `initialize` and `tools/call` (`resolve-library-id`, `query-docs`) from this VM **without an API key** using the pinned Python `mcp` client (`streamable_http_client`, two-value context) | manual run during design; stored live receipt `docs/evidence/context7-live-*.json` |
| Context7 accepts `Authorization: Bearer <key>` for authenticated use | Context7 documentation retrieved through the Context7 MCP (`/upstash/context7`, all-clients.mdx) |
| bubblewrap works unprivileged on the development VM; `config/verifiers.json` as committed pointed at `/workspace/omo/...`, which made 67 unit tests fail in a relocated checkout | `pytest tests/unit` before stage 0: 67 failed, 222 passed |
| The committed MCP tool surface is exactly 8 tools | `omo_grok/mcp_server.py` `TOOL_NAMES`, `tests/integration/test_mcp_stdio.py` |
| Grok Bot hosting limits (one shared computer and account, no native hooks, no `spawn_agent`, no per-role models, routines are the only wake-up) | original spec s0, s2; `docs/SECURITY.md` |

## 3. Requirements

Each requirement has an identifier used by the acceptance matrix (section 11) and by the tests.

- **R1 Intent gating.** Hephaestus classifies every request as `explanation`, `investigation`, or `implementation`
  and passes it to `omo.submit`. The engine enforces the consequences. `explanation` creates no job and no task.
  `investigation` is read-only: it can never produce an executor task, a worktree, an approval with `allow_edit`, or
  a candidate revision. Only `implementation` can edit. The engine, not the prompt, refuses edits for read-only intents.
- **R2 Smallest adequate team.** For `implementation` the engine computes a minimum class (`quick`, `standard`,
  `heavy`) from a validated scope attestation. Hephaestus may choose a larger class, never a smaller one.
  `none` is the class of `explanation` (no job). Each class has a fixed, tested task set (section 5).
- **R3 Safe escalation.** Classes only go up. Escalation adds the missing research tasks, re-plans, and revokes any
  approval. The engine escalates automatically when the real diff contradicts the attestation. It is never possible
  to escalate down.
- **R4 Continuity.** Every job has an active-plan markdown file and a `.omo/boulder.json` projection (upstream
  shape, section 6) containing work, session, and task tracking. A restart (new process, same database) resumes the
  same job and plan; an identical active request returns the existing job instead of creating a duplicate.
- **R5 Single authority.** The database is the only source of transitions. The boulder file is write-only for omo-grok:
  it is regenerated from the database, never read to decide anything.
- **R6 Projection integrity.** The projection is written under an exclusive file lock, rendered from a consistent
  database snapshot inside the lock, replaced atomically, and never regresses. Concurrent writers cannot lose an
  update; crashes leave either the old or the new complete file; tampering is repaired and logged, never imported and
  never a reason to block a job.
- **R7 Real LSP.** Diagnostics gating uses an LSP client against real servers, run inside the existing bubblewrap
  sandbox, bound to the exact git blob contents, proven by a canary, and judged as a base-versus-candidate delta
  (section 7).
- **R8 Fail-closed unavailability.** Any code file in a changed set without a configured, working diagnostic profile
  makes the gate fail with `diagnostics_unavailable`. It never passes silently, and unsupported languages are listed
  by name, not hidden behind a Python-only implementation.
- **R9 Evidence that is not self-report.** A gate may be satisfied only by `engine_executed` or `engine_reverified`
  evidence (section 8). Agent statements that a tool ran never satisfy a gate. The engine never invents a receipt,
  a quote, or a tool result.
- **R10 Context7.** The engine can call Context7 itself (real connector). The call is proven by the engine's own
  receipt (request, server identity, response hash, stored response). A bot's citation is valid only if it names a
  receipt of this job and its quote occurs in that receipt's stored response. A re-fetch is never treated as proof
  that a bot called a tool.
- **R11 git-master.** The executor packet carries the git-master skill text; the engine records its hash as
  `engine_delivered` and checks the structural consequences in git history (section 9). The engine states that
  delivery does not prove the model read or obeyed it.
- **R12 Gates preserved.** Momus review of the exact plan hash, user approval, independent code review and QA on the
  exact candidate revision, the revision-bound final gate, bounded repair cycles, and invalidation of reviews on a new
  revision all behave exactly as before for every class that can edit.
- **R13 Relocation and bootstrap.** A fresh clone can be made runnable from nothing by `scripts/bootstrap.sh`. No path
  to `/workspace/omo` is baked in. `omoctl doctor` reports precisely what is missing.
- **R14 Conservative flags.** Every new behaviour sits behind a per-feature mode (`off`, `shadow`, `enforce`) that
  defaults to `off`. The mode is frozen into the job at submit, so changing configuration cannot alter a running job.

## 4. Intent gating

### 4.1 Where classification happens

Natural-language classification is done by the Hephaestus model, guided by `agents/hephaestus-omo.md`. It is a
prompt-level routine and is not claimed to be deterministic. What is deterministic is what the engine does with the
value, so a wrong "explanation" can only cause too little work, never an edit.

### 4.2 Engine rules (all enforced in code, all tested)

1. `submit(intent="explanation", ...)` returns `{"created": false, "roster_class": "none", "action": "answer_directly"}`,
   writes one `intent_gated` event with no job id, creates no job row and no task row.
2. `submit(intent="investigation", ...)` forces mode `plan_only`; `mode="implement"` is refused with
   `ValidationError`. The job's task set is `explore` (always), `research` (only if `scope.external_api`), `analyze`.
   After `analyze` passes, the engine marks the job `done` (SUP-4) with assurance `read_only_investigation`.
3. `_new_task` refuses `implement`, `code_review`, `qa`, `integrate` for any job whose intent is not `implementation`.
4. `approve` refuses `allow_edit=true` for a job whose intent is not `implementation`.
5. `_start_implementation` refuses a job whose intent is not `implementation`.
6. A job submitted without an intent keeps the pre-existing behaviour while the `adaptive_roster` mode is `off`. In
   `enforce`, a missing intent is a `ValidationError`.

## 5. Adaptive roster

### 5.1 Scope attestation

`scope` is a validated object: `files_estimate` (int 1..10000), `new_dependency` (bool), `external_api` (bool),
`security_sensitive` (bool), `cross_cutting` (bool). Unknown keys, wrong types, and out-of-range values are rejected.
The attestation is stored with the job and is immutable; escalation records a new row, never edits the old one.

### 5.2 Floor computation (deterministic)

```
heavy    if security_sensitive or new_dependency or files_estimate > 15
standard if cross_cutting or external_api or files_estimate > 3
quick    otherwise
```

`roster_class` omitted selects the floor. A requested class below the floor is refused with the floor and the
triggering fields in the error. A class above the floor is allowed.

### 5.3 Task sets

| Class | Tasks created at submit | Notes |
|---|---|---|
| none | none | explanation only |
| quick | `plan` -> `plan_review` | `explore`, `research`, `analyze` absent (SUP-1, SUP-2) |
| standard | `explore` -> `analyze` -> `plan`; plus `research` when `external_api` | |
| heavy | `explore`, `research`, `analyze`, `plan` | identical to the original DAG |

After approval the implementation chain (`implement`, verifier, `code_review` and `qa`, `integrate`, final gate) is
identical for all classes.

### 5.4 Escalation

`Engine.escalate(job_id, to_class, reason, actor)` and the MCP tool `escalate` accept only a class strictly above the
current one and only while the job is not terminal. Effects, in one transaction: set the class, append an
`escalations` row, create the missing research tasks (`explore`, `research`, `analyze` as the target class requires),
revoke any live approval, supersede the current plan revision (if any) and cancel unstarted implementation-side
tasks, and move the job back to the earliest incomplete phase. Automatic escalation runs when an `implement` task
completes: if the real diff (`base..commit`) touches more files than attested by a factor of two, or touches a
sensitive path (`requirements*.txt`, `*.lock`, `pyproject.toml`, `package.json`, `Dockerfile`, `.github/`, anything
matching `secret|credential|auth` in the path) while `security_sensitive`/`new_dependency` were attested false, the
candidate is rejected with a recorded reason, the class steps up one level, approval is revoked, and the job re-plans.
This path never edits or deletes the candidate commit.

## 6. Continuity: boulder and active plan

### 6.1 Files

Per job, under `jobs/<job_id>/.omo/` (outside the product repository so a candidate commit can never contain it, and
so no candidate-controlled path can overwrite it): `boulder.json`, `boulder.lock`, `plans/<plan_name>.md`, and a
self-ignoring `.gitignore` as upstream writes. The deliberate difference from upstream (worktree-local boulder) is
that omo-grok jobs may have no worktree (investigation, planning) and that the worktree is the executor's
writable surface.

### 6.2 Boulder schema (upstream-shaped, superset)

`schema_version: 2`, `active_work_id`, `works{work_id: {work_id, active_plan, plan_name, status, started_at,
ended_at?, updated_at, session_ids[], session_origins{}, agent, worktree_path?, task_sessions{task_key: {task_key,
task_label, task_title, session_id, agent, category, started_at, ended_at?, status, updated_at}}}}` plus the mirrored
root fields exactly as upstream, plus an extension object `x_omo_grok`:
`{"job_id", "generation", "projection": "derived-from-sqlite", "resume": {"tool": "omo.status", "job_id": ...},
"roster_class", "intent", "phase", "plan_hash", "candidate_revision", "next_action"}`.

Status mapping: phases `research`..`running` and `awaiting_approval` -> `active`; `blocked` -> `paused`; `done` ->
`completed`; `cancelled` and `failed` -> `abandoned`. Session ids are `omo-grok:<bot_id|role>:<attempt_id>`. The
format is intentionally not an upstream platform prefix because no Grok Bot platform session id is observable.
The file is shape-compatible with the verified upstream type; it is not claimed to be consumed by upstream tools.

### 6.3 Active plan markdown

Rendered with the verified upstream checklist grammar: `## TODOs` containing `- [x] 1. explore ...` rows (one per
task, `[~]` for blocked, `[x]` for passed), then `## Final Verification Wave` with `F1` (verifier), `F2` (code
review), `F3` (QA), `F4` (final gate). The approved plan text is appended under `## Plan (hash <sha256>)`.

### 6.4 Writer protocol (R5, R6)

1. Single writer function `projection.project(conn, paths, job_id)`. Nothing else writes these files.
2. Take an exclusive `flock` on `boulder.lock` with a bounded wait (default 10 s). On timeout: record
   `projection_status=stale` with `last_error`, emit `projection_stale`, return. The caller never fails.
3. Inside the lock, open a read transaction, read the job, tasks, sessions, plan revision and approval, and compute
   `generation = MAX(events.id WHERE job_id=?)` from the same snapshot, render deterministically, close the snapshot.
4. If the existing file's generation is greater than the rendered generation, do nothing (never regress). If equal and
   the bytes are identical, do nothing.
5. Write a temp file in the same directory (0600), `fsync`, `os.replace`, `fsync` the directory. No partial file is
   ever visible. No database write transaction is open during file I/O; the lock order is file lock first, then at
   most one short transaction to record `{generation, sha256, status}` in `boulder_projection`.
6. If the file's sha256 differs from the recorded one before a write (edited, truncated, deleted), record a
   `boulder_tamper` event and overwrite from the database. A tampered file never changes any job state and never
   blocks dispatch.
7. Triggers: every public `Engine` mutation flushes the projections of the jobs whose events it wrote, after the
   outermost call returns (outside every transaction).
8. Sessions are rows (`work_sessions`, unique `session_token`), not file fields, so concurrent appends cannot be lost
   by a read-modify-write.

### 6.5 Restart and resume

`Engine.status()` with no argument returns `active_work`: every non-terminal job with `job_id`, `work_id`,
`plan_name`, `phase`, `next_action`, `boulder_path`, and per-task sessions. `omo.reconcile` (and the
`omo-reconcile` routine) re-dispatches queued and expired work idempotently. `submit` with the same
`requirements_hash` as a non-terminal job returns that job (`created=false`, `resumed=true`) even under a different
idempotency key. A blocked job resumes only through `omoctl resume`. The `agents/hephaestus-omo.md` instruction
requires `omo.status` at the start of every turn and before any `submit`. Grok Bot has no stop hook, so there is no
automatic continuation: resume is driven by (a) that status-first rule, (b) the optional reconcile routine, and (c)
`awaiting_resume` claims.

## 7. Real LSP integration

### 7.1 Client

`omo_grok/lsp/client.py` implements Content-Length framed JSON-RPC over the server's stdio, with request
correlation, a reader thread, bounded message sizes, and handlers for server-to-client requests
(`workspace/configuration` answered from the profile `settings`, `client/registerCapability`,
`window/workDoneProgress/create` answered with null; any other request answered with MethodNotFound so a server never
hangs). Lifecycle: `initialize` (with `workspaceFolders`, `publishDiagnostics.versionSupport`, `diagnostic`
capability), `initialized`, per file `didOpen` (version 1, full text), diagnostics collection, `didClose`,
`shutdown`, `exit`, bounded wait for process exit.

### 7.2 Diagnostics collection

- **Pull**: if `capabilities.diagnosticProvider` exists, send `textDocument/diagnostic`; accept a Full report; treat
  Unchanged as an error because nothing was cached.
- **Push**: otherwise wait for `textDocument/publishDiagnostics` for the file URI. If a `version` is present it must
  equal the sent version; messages with another version are ignored. Collection ends after a profile-defined quiet
  period with no further message for that file, or at the per-file deadline, which is a failure, not "no diagnostics".

### 7.3 Sandbox and inputs

The server runs under `sandbox.wrap`-style bubblewrap (no network, private `/tmp`, scrubbed environment) with the
revision's files staged read-write at `/work` from `git archive` (never the user's worktree) and the tools directory
bound read-only at `/omo-tools`. The sandbox, the tool paths, and the staged tree are inputs of the receipt.
`didOpen` text hashes are compared with the git blob ids of the revision (`git ls-tree`), so what the server saw is
bound to the exact revision. Engine-owned configuration: the profile's `config_files` are taken from the **base**
revision for both runs; a candidate that adds, edits or deletes one of them fails `lsp_config_untouched`.

### 7.4 Canary (positive control)

Before analysing repository files, in the same server session, the client opens a known-bad file and a known-good
file written to the scratch directory (never part of the repository). The run is valid only if the bad file yields at
least one diagnostic with a code in the profile's `expect_codes` and the good file yields no error-severity
diagnostic. A server that is missing, silent, stubbed, or crashed fails the canary and the receipt is `unavailable`.

### 7.5 Delta

Diagnostics are keyed by `(path, severity, code, normalised message)` as multisets, so line drift does not create
false positives. `new = candidate - base` (per key counts). Gate: no new diagnostic of a severity in the profile's
`fail_on` (default `error`). Scope is every profile file in the revision up to `max_files` (default 400); beyond that
the scope is the changed files only and the receipt says so. New suppression comments in added lines (profile
`suppression_patterns`) fail `lsp_no_new_suppressions`.

### 7.6 Language coverage (explicit)

| Language | Server | Mode | Status |
|---|---|---|---|
| Python | `ruff server` | pull | implemented and verified against the real server in tests |
| Python | `basedpyright-langserver --stdio` | push (versioned) | implemented and verified against the real server in tests |
| TypeScript / JavaScript | `typescript-language-server --stdio` + `typescript` | push | implemented and verified against the real server in tests |
| anything else | none | n/a | **unsupported**: a changed file with a known code extension and no working profile fails with `diagnostics_unavailable` (R8). Profile templates for other servers may be added to `config/lsp.json` but are disabled and unverified. Documentation files (md, json, yaml, txt...) are not code. |

This spec never claims general language coverage. The list above is the entire verified set.

## 8. Evidence levels (R9)

| Level | Meaning | May satisfy a gate |
|---|---|---|
| `engine_executed` | The engine ran the tool (or made the network call) and recorded argv/endpoint, tool identity and version, input hashes, output hashes and stored transcript | yes |
| `engine_reverified` | The engine re-checked a bot-supplied claim against engine-held evidence (a citation quote against a stored engine receipt) | yes |
| `engine_delivered` | The engine placed a text in a bot's packet and recorded its hash | no |
| `agent_claimed` | A bot's assertion of tool use | no |

Stored in `tool_receipts` (append-only by convention; same-account bots can still edit the database, as in the original
s4/s11 caveat). The only code that inserts receipts is in `omo_grok/engine.py` helpers called by engine-run tools; the
MCP `complete` argument schema has no receipt field and unknown keys are rejected.

Honest limits: (a) a re-fetch proves what a source says now, not that a bot called a tool, so it is only used to bind
quotes to receipts the engine itself created; (b) receipts are tamper-evident to cooperating agents, not tamper-proof
against a hostile same-account bot; (c) the true hard boundary is an external CI check in the target repository.

## 9. git-master and skills

`skills/git-master/SKILL.md` is written for this project (it is not a copy of upstream text; the upstream skill body
was not located at the pinned tree, so only its documented configuration surface, an env prefix, an optional
co-author footer, and a footer string, informed the trailer rule). The executor packet field `skill_pack` carries
`[{name, sha256, text}]`. On claim the engine records an `engine_delivered` receipt with the pack hash and the
attempt id. Structural consequence checked by the final gate when `git_discipline` is not `off`:
history `base..candidate` is linear (no merge commits), has at most `max_commits` (default 50) commits, and every
commit message carries the trailer `Omo-Job: <job_id>`; commits are on the job branch only.
**Limit:** delivery and a conforming history do not prove the executor read or understood the skill.

## 10. Feature flags (R14)

`policy.json` key `features`, each `off | shadow | enforce`: `adaptive_roster`, `boulder` (`off | on`), `lsp`,
`git_discipline`, `docs_evidence`, `repo_lease`. All default `off`. `shadow` computes and records the decision as an
event without changing behaviour. `enforce` changes behaviour. The effective set is copied into the job's
`policies_json.features` at submit; running jobs ignore later configuration changes. `omoctl features` prints the
effective and per-job values. Intent-based refusals for read-only intents (section 4.2 rules 2 to 5) apply whenever
an `intent` is supplied, independent of flags, because they only remove capability.

## 11. Acceptance matrix

| Requirement | Tests (all automated, real code paths) |
|---|---|
| R1 | `tests/integration/test_adaptive_roster.py::test_explanation_*`, `::test_investigation_*`, `::test_read_only_*` |
| R2 | `::test_floor_*`, `::test_quick_task_set`, `::test_standard_*`, `::test_heavy_equals_legacy_dag` |
| R3 | `::test_escalate_*`, `::test_auto_escalation_on_diff_*` |
| R4, R5 | `tests/integration/test_continuity.py` (restart/resume, duplicate, boulder never read) |
| R6 | `tests/unit/test_projection.py`, `tests/integration/test_projection_concurrency.py` (multi-process, crash points, tamper) |
| R7, R8 | `tests/unit/test_lsp_client.py` (fake server, protocol), `tests/integration/test_lsp_real_servers.py` (ruff, basedpyright, typescript-language-server: canary and delta), `tests/integration/test_lsp_gate.py` |
| R9, R10, R11 | `tests/integration/test_evidence.py`, `tests/unit/test_context7.py` (local MCP server), `tests/integration/test_context7_live.py` (live, opt-in env) |
| R12 | existing `test_workflow.py` suite unchanged and green, plus `tests/integration/test_real_repo_exercise.py` (controlled defect, failure then repair, invalidated reviews, independent reviewer) |
| R13 | `tests/unit/test_relocation.py`, `scripts/bootstrap.sh` run in a clean copy |
| R14 | `tests/unit/test_features.py` |

## 12. Operational state and doctor

`omoctl doctor` reports `NOT OPERATIONAL` unless the database holds (a) a `gate_results` row with `passed=1` for an
`implement`-mode job on a non-fixture repository that has engine-executed LSP and git receipts, (b) the recorded
host cutover attestation (`omoctl attest`, human TTY, stores who/when/what, never a secret), and (c) the original
section 10 items 20 and 21 live-evidence records. The synthetic fixture and the disposable-repository exercise in this
branch satisfy none of these on their own, so this branch can only produce `NOT OPERATIONAL` or `PARTIALLY INSTALLED`.

## 13. Residual assumptions and limits (not verified here)

- Grok Bot cannot be exercised from this environment; host MCP registration and routine behaviour are untested here.
- Context7 rate limits for the anonymous path, and the behaviour of an expired or wrong key, are not characterised.
- The git-master skill text is original to this repository.
- Sandbox behaviour on kernels or runners that restrict unprivileged user namespaces is "unavailable, fail closed".
- Timing of language servers on very large repositories is bounded by timeouts and `max_files`, not benchmarked.
- Same-account bots can edit the SQLite file, the receipts, the boulder, and this repository. See `docs/SECURITY.md`.
