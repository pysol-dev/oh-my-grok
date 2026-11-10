# Adaptive Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: executing-plans (inline) with test-driven-development. Steps use
> checkbox syntax. Each stage ends green, committed, and verified before the next starts.

**Goal:** Implement `spec/ADAPTIVE.md`: intent-gated adaptive rosters, locked atomic boulder projection with restart
semantics, a real LSP diagnostics gate, engine-proven tool evidence (LSP, Context7, git), reproducible bootstrap.

**Architecture:** SQLite stays the only transition authority. New behaviour is added as small modules
(`roster.py`, `projection.py`, `lsp/`, `context7.py`, `receipts.py`, `skills.py`, `features.py`) called from
`engine.py` at fixed points. Everything new is behind per-feature modes that default to `off`.

**Tech stack:** Python >= 3.12, SQLite (stdlib), `mcp` 2.3.0 (pinned), bubblewrap, `ruff` 0.17.4,
`basedpyright` 1.40.2, `nodejs-wheel-binaries`, `typescript` 5.9.3, `typescript-language-server` 5.3.0.

## Global constraints (copied from the original spec and the task)

- `spec/IMPLEMENTATION.md` byte-identical (sha256 `9643ee06...0d30ae`).
- No secrets in logs, status, receipts, artifacts, or fixtures. Never read, rotate, or print keys.
- No `/workspace/omo` in shipped code, tests, or config (SUP-8).
- Never run candidate code outside the sandbox. Never `sudo` in bootstrap. No merge or deploy.
- Feature modes default `off`; tests that need behaviour set `enforce` explicitly.
- Status stays NOT OPERATIONAL until the original s13 live evidence exists.
- Do not touch the live Grok Bot runtime, routines, hooks, profile, or other repositories.

## File map

| Path | Responsibility |
|---|---|
| `spec/ADAPTIVE.md` | the sibling spec (this plan implements it) |
| `omo_grok/features.py` | feature modes, validation, per-job freeze |
| `omo_grok/roster.py` | intent, scope attestation, floor, task sets, escalation rules (pure functions) |
| `omo_grok/projection.py` | boulder + active plan renderer and the locked atomic writer |
| `omo_grok/receipts.py` | `tool_receipts` insertion helpers and evidence level constants |
| `omo_grok/lsp/{client,profiles,runner,delta}.py` | JSON-RPC client, profile loading, sandboxed run with canary, delta |
| `omo_grok/context7.py` | Context7 MCP client wrapper producing receipts |
| `omo_grok/skills.py` | skill pack loading and hashing |
| `skills/git-master/SKILL.md` | executor skill text |
| `config/lsp.json` | LSP profiles (python, typescript) |
| `scripts/bootstrap.sh`, `requirements-tools.lock`, `tools/node/package*.json` | reproducible tool install |
| `omo_grok/db.py` | migration v5 (new tables and columns) |
| `omo_grok/engine.py` | calls into the new modules; `escalate`, `docs`, projection flush, gate checks |
| `omo_grok/mcp_server.py`, `omo_grok/cli.py` | new tools and commands |
| `agents/hephaestus-omo.md`, `routines/*.md` | instruction updates |
| `tests/...` | per stage, listed below |

## Stage 0: spec, plan, relocation baseline

- [x] 0.1 Write `spec/ADAPTIVE.md` and this plan; `tests/unit/test_spec_integrity.py` hashes the original spec.
- [x] 0.2 RED: `tests/unit/test_relocation.py` asserts no shipped file contains `/workspace/omo` and that
  `load_verifier_profiles` expands `${OMO_PYTHON}` / `${OMO_CODE_ROOT}`. Run: fails.
- [x] 0.3 GREEN: placeholder expansion in `config.py`; `config/verifiers.json`, `config/mcp-command.txt`,
  `omoctl`, tests, docs updated; `omoctl mcp-command` prints the computed command; `scripts/bootstrap.sh`.
- [x] 0.4 Verify: `tests/unit/test_verifier.py` (previously 67 failures in a relocated checkout) passes.
- [x] 0.5 Commit.

## Stage 1: features, schema v5, intent and roster

- [x] 1.1 RED `tests/unit/test_features.py`: defaults off, unknown names and modes refused, per-job freeze.
- [x] 1.2 RED `tests/unit/test_roster.py`: floor table, attestation validation, class ordering.
- [x] 1.3 GREEN `features.py`, `roster.py`, migration v5 (`jobs.intent`, `jobs.roster_class`, `jobs.scope_json`,
  `escalations`, `tool_receipts`, `work_sessions`, `boulder_projection`, `repo_leases`).
- [x] 1.4 RED `tests/integration/test_adaptive_roster.py`: explanation creates nothing; investigation read-only in all
  four enforcement points; quick/standard/heavy task sets; heavy equals the legacy DAG; below-floor refused;
  escalation adds tasks, revokes approval, re-plans; auto-escalation on a contradicting diff; legacy path unchanged
  with flags off.
- [x] 1.5 GREEN engine changes (`submit`, `_new_task`, `approve`, `_start_implementation`, `_on_*`, `escalate`).
- [x] 1.6 Verify: new tests plus `tests/integration/test_workflow.py`, `test_dispatch.py`. Commit.

## Stage 2: continuity and projection

- [x] 2.1 RED `tests/unit/test_projection.py`: schema fields (upstream shape), checklist grammar, status mapping,
  deterministic bytes, never regress, tamper repair, no import.
- [x] 2.2 RED `tests/integration/test_projection_concurrency.py`: N processes each performing engine mutations on one
  job; reader process asserts the file always parses and `generation` never decreases; kill -9 at injected crash
  points leaves a parseable file and converges on the next flush; flock timeout marks stale without failing the job;
  ENOSPC-style failure (read-only directory) marks stale without failing the job; concurrent session rows all survive.
- [x] 2.3 RED `tests/integration/test_continuity.py`: restart (new Engine on same DB) resumes the same job and plan,
  identical active request returns the same job, `status()` reports `active_work`, blocked jobs resume only by
  `resume`, file edits/deletes never alter the database, repo lease refuses a second implement job.
- [x] 2.4 GREEN `projection.py`, engine flush hook, `work_sessions` writes in `_insert_claim`, `active_work`.
- [x] 2.5 Verify and commit.

## Stage 3: real LSP

- [x] 3.1 `scripts/bootstrap.sh` tool install; verify the three servers start (manual probe already recorded in
  `docs/evidence/`).
- [x] 3.2 RED `tests/unit/test_lsp_client.py` with a scripted fake server (protocol framing, pull and push modes,
  version filtering, server requests, timeouts, crash, malformed frames, oversize).
- [x] 3.3 RED `tests/unit/test_lsp_delta.py`: multiset delta, line-drift immunity, severities, suppressions, config files.
- [x] 3.4 RED `tests/integration/test_lsp_real_servers.py`: ruff (pull), basedpyright (push), typescript (push) real
  canary; real delta (base clean, candidate adds a type error); unavailable server (missing binary, stub that exits 0
  and is silent, stub that answers initialize only) all fail closed.
- [x] 3.5 GREEN `lsp/` package and sandbox wrapper for long-lived stdio children.
- [x] 3.6 RED `tests/integration/test_lsp_gate.py`: candidate verification and final gate consume receipts; a type
  error that unit tests miss rejects the candidate and opens a repair cycle; unavailable diagnostics block;
  `shadow` records without blocking; unsupported code extension blocks.
- [x] 3.7 GREEN engine integration (candidate verification, final gate checks `lsp_delta_clean`,
  `lsp_config_untouched`, `lsp_no_new_suppressions`).
- [x] 3.8 Verify and commit.

## Stage 4: evidence, Context7, git-master

- [x] 4.1 RED `tests/unit/test_context7.py` against a local MCP server (real protocol), covering receipt contents,
  failure to connect, key never logged; `tests/integration/test_context7_live.py` (opt-in `OMO_LIVE_CONTEXT7=1`)
  stores `docs/evidence/context7-live-*.json`.
- [x] 4.2 RED `tests/integration/test_evidence.py`: fabricated receipt ids, fabricated quotes, agent-claimed evidence,
  `complete` payloads naming receipts, missing receipts under `enforce` all fail; skill pack hash recorded on claim;
  git history shape (merge commit, missing trailer, too many commits) fails.
- [x] 4.3 GREEN `receipts.py`, `context7.py`, `skills.py`, `skills/git-master/SKILL.md`, engine checks, MCP `docs`.
- [x] 4.4 Verify and commit.

## Stage 5: surfaces and instructions

- [ ] 5.1 MCP tools `escalate`, `docs`; `submit` arguments; `tests/integration/test_mcp_stdio.py` updated.
- [ ] 5.2 CLI: `lsp`, `boulder`, `features`, `mcp-command`, `docs`, `receipts`, `attest`; `doctor` derives its verdict.
- [ ] 5.3 Instructions: `agents/hephaestus-omo.md`, executor, librarian, explorer, reconcile routines (versioned).
- [ ] 5.4 Docs: ARCHITECTURE, SECURITY, RUNBOOK, MIGRATION, README, PROGRESS.
- [ ] 5.5 Verify and commit.

## Stage 6: real-repository exercise

- [ ] 6.1 Build a disposable copy of this repository (tracked files only, new git history) with a controlled defect in
  a text-processing module and a unittest suite that exposes it.
- [ ] 6.2 RED/GREEN `tests/integration/test_real_repo_exercise.py`: job through the adaptive classes; attempt 1 fails
  verification; attempt 2 passes tests but introduces a type error that only the real language servers see and is
  rejected; attempt 3 is correct; reviews on earlier revisions are invalidated; reviewers are different Bots from the
  executor; the final gate passes with LSP and git receipts; evidence files exported.
- [ ] 6.3 Off-box CI workflow `.github/workflows/ci.yml` that runs bootstrap and the suite.
- [ ] 6.4 Verify, commit, push, open PR.

## Final verification (every stage re-run)

```
OMO_REQUIRE_TOOLS=1 .venv/bin/python -m pytest -q
```
Record counts, exit codes, durations in `docs/evidence/test-run-*.txt`.

## Self-review against the requirements (completed before stage 1)

| Requirement from the task | Plan coverage |
|---|---|
| Intent gating, no edit for read-only intent | Stage 1 (spec s4.2, four enforcement points) |
| Smallest adequate team, validated none/quick/standard/heavy, safe escalation | Stage 1 (spec s5) |
| Boulder continuity, active-plan markdown, session/work/task tracking, restart semantics | Stage 2 (spec s6) |
| Locked atomic projection, no lost updates, DB sole authority | Stage 2 (spec s6.4, multi-process and crash tests) |
| Real LSP, not a renamed checker; fail-closed unavailable; explicit language scope | Stage 3 (spec s7, s7.6) |
| git-master delivery with accurate proof limits | Stage 4 (spec s9) |
| Context7 real connector, meaningful evidence, no invented quotes, no re-fetch-as-proof | Stage 4 (spec s8, R10) |
| Verify unresolved v2 assumptions | Done before this plan; recorded in spec s2; basedpyright is push-only, which changed the design |
| Momus plan review, approval, exact-revision final gate | unchanged; asserted by existing and new tests (R12) |
| Host tool identity boundaries, shared-account limits | spec s8 limits; SECURITY.md update in 5.4 |
| Tests listed by the task | acceptance matrix in spec s11 |
| Real non-arithmetic repository exercise | Stage 6 |
| Bootstrap, relocation-safe config, host-side needs | Stage 0 and 5.4 |
| Flags conservative, implemented is not operational | spec s10, s12 |
| PR, commands, evidence files, language coverage, blockers | final report |

Gaps found in self-review and fixed in the spec: (1) the project had no spec-integrity test, added in 0.1; (2) an
earlier design said a boulder mismatch blocks dispatch, replaced by regenerate-and-log; (3) `docs` and `escalate`
were not in the supersession register, added as SUP-3; (4) the investigation path to `done` needed an explicit
supersession (SUP-4) and four enforcement points.
