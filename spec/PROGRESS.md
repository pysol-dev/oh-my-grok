# omo-grok implementation ledger (authoritative; resume from here)

Spec: /workspace/omo/spec/IMPLEMENTATION.md — 54470 bytes, sha256 9643ee06f6817b08c57ea585fd8f919d4b23639bffd817a4a3084c7c9d0d30ae (verified 2026-11-03 20:40 MST; first section "MANDATORY PREFACE" and last line "Implement now..." present).
Owner: Hephaestus (bot f497e4a7-5d7d-46e4-829f-b0da987cbac4).

## Recon findings (T00, done 2026-11-03)
- /workspace/omo did NOT exist before this run. No bus.py, omoctl, omo SQLite DB, or hooks.json anywhere on the box.
- The previously claimed SQLite build at /workspace/omo is NOT reproducible: nothing to back up, no tests to rerun. Recorded as an unverified/false prior claim.
- Existing Bots (preserve, never delete/rename): Hephaestus f497e4a7, Metis 41b28a72, Momus 42b0d03a, Librarian 78a94f3f, Explorer dbbe6828, Executor e3dfae0e, plus unrelated bots (dr eggbot, Spork, GitHub, Citrate, Architecture Research, Architect, research, Github Goonsquad).
- No existing omo routines on Hephaestus. Python 3.13.5, git 2.47.3.

## Tasks
| ID | Task | Depends | Acceptance | Status |
|---|---|---|---|---|
| T00 | Persist spec, verify hash, inventory, ledger | - | hash matches, ledger exists | DONE |
| T01 | Backup of prior build | T00 | verified backup or documented "nothing to back up" | DONE (nothing existed; documented) |
| T02 | SQLite domain model + migrations + unit tests (spec §4) | T00 | `omoctl test unit` passes | DONE 2026-11-03 21:11 MST (82 unit passed, exit 0) |
| T03 | Pinned MCP stdio server `omo` + omoctl CLI (§5,§6) | T02 | JSON contract + multiprocess tests pass | DONE locally (SDK stdio client test + subprocess tests pass). NOT registered in Grok Bot (T08) |
| T04 | Outbox dispatcher, leases, retries, reconcile (§7) | T02 | mock-HTTP tests 7,8,22 pass | DONE for 7,8 (loopback HTTP server + mocks). Item 22 is LIVE: harness only, NOT RUN |
| T05 | Artifacts, plan hashes, approvals, worktrees, verifier profiles, fixture, final gate (§8,§9) | T02-T04 | spec §10 tests 1-19 pass | DONE: items 1-19 automated, 40 integration passed, exit 0. Approval is SOFT only; strict/external approval documented, not implemented |
| T06 | Docs: ARCHITECTURE, SECURITY, RUNBOOK, MIGRATION, README; routines/<role>.md; agents/ | T05 | files exist and match code | DONE (routines are drafts, not created in any Bot) |
| T07 | Independent review of T02-T06 by Momus | T06 | Momus pass with evidence | IN REVIEW: Momus FAILED d3ac281 and b7cdc15; round-2 findings (G/D/E/F + test hygiene) fixed in follow-up commit; re-review #3 by Momus pending |
| T08 | Register `omo` MCP in Grok Bot (command server); confirm in >=2 Bots | T07 | real tool call readback from 2 bots | TODO |
| T09 | Role→Bot→routine registry with real bot ids; webhook routines omo-bus-<role> created per bot | T08 | routines read back | TODO |
| T10 | Webhook URL/key collection via masked `omoctl hook set` (user-only step) | T09 | hook list shows configured | TODO (likely USER BLOCKER) |
| T11 | Live smoke (§10.20) + live e2e fixture (§10.21) + negative live (§10.22) | T10 | real run receipts + DB events | TODO |
| T12 | Cutover/rollback docs; final report per §14 | T11 | report delivered | TODO |

## Log
- 2026-11-03 20:40 MST: T00/T01 done by Hephaestus.
- 2026-11-03 20:45 MST: T02–T06 dispatched to a background build worker (high effort). Live path notes: MCP registration = in-app AddMcpServer with local command (needs user confirm widget; applies account-wide to all bots). Webhook routines for other bots must be created by those bots (SendToAgent); webhook URL/key is user-only via routine UI -> masked `omoctl hook set`.
- 2026-11-03 20:45 MST: executor subagent started T02-T06. venv /workspace/omo/.venv with mcp==2.3.0 (official SDK; FastMCP renamed MCPServer in 2.x), pytest. git repo initialised (no remote).
- 2026-11-03 21:11 MST: executor subagent finished T02-T06. Results:
  - `./omoctl doctor`: exit 0, prints "NOT OPERATIONAL until live cross-Bot evidence exists".
  - `./omoctl test unit`: 82 passed, exit 0.
  - `./omoctl test integration`: 40 passed, exit 0. Covers spec §10 items 1-19, the SDK stdio client, real subprocess concurrency, and a loopback HTTP webhook server.
  - `./omoctl test live --fixture-only`: exit 9, NOT RUN. Missing prerequisites: no bindings or hooks.
- Honest gaps (open):
  - Live items 20-22 have not been run; harness only (omo_grok/live.py).
  - Bot ids in config are UNVERIFIED (from the parent brief).
  - No MCP registration, routines, webhook keys or scheduled reconciler have been created.
  - Approval is SOFT only. The strict external-signature protocol is documented in SECURITY.md but not implemented, so production merge/deploy fails closed.
  - No Auto Review rule was created.
  - requirements.lock was checked with `pip install --require-hashes --dry-run` (exit 0) but has not been installed into a fresh venv.
- 2026-11-03 21:14 MST: T02–T06 reported DONE by build worker (commit d3ac281; doctor OK; unit 82/0/0; integration 40/0/0; live NOT RUN). T07 started: verification request sent to Momus (42b0d03a), awaiting PASS/FAIL. T08 awaiting user yes for AddMcpServer.
- 2026-11-03 21:31 MST: T07 Momus review FAILED d3ac281. Findings and fixes:
  - BLOCKING verifier bypass (committed `unittest/` package shadowing stdlib): verifier now runs Python isolated (`-I`, PYTHONSAFEPATH, PYTHONNOUSERSITE; config validation refuses non-isolated python profiles) AND statically rejects candidates before execution: changes outside per-profile `editable_paths`, protected paths, hook files (sitecustomize/usercustomize/.pth/conftest.py/runner config), top-level modules shadowing stdlib/verifier/site-packages names; renames counted (`--no-renames`); missing base fails closed. Regression tests: 11 attack variants + isolation-only layer + old-argv-would-pass + engine-level attack (item 12).
  - BLOCKING pyflakes: doctor import checks rewritten with importlib; `pyflakes omo_grok tests` exit 0.
  - MINOR cancel: all confirmation checks inside the BEGIN IMMEDIATE txn (race test with concurrent approval).
  - MINOR artifacts: inline artifacts content-addressed (task/aN/sha16/name), atomic temp+link publish, never unlinked; tests for rolled-back submit, retry, tampered path.
  - MINOR status URL leak: dispatch errors scrubbed of URL/key at write time and every URL cut to scheme://host on write and display; works without loaded hooks (fresh-process test).
  - MINOR export: lease_token/claim_token redacted (test).
  - Found while fixing: implement packets had null plan_revision (vacuous item3 assert hid it) -> fixed; implementation-side claim/checkpoint/complete did not re-check approval -> now require a live approval of the CURRENT plan revision, and revocation cancels unstarted implementation tasks; MCP SDK argument-validation errors echoed rejected input values -> server now returns field names only.
  - TEST QUALITY: item 8 restart is a real separate OS process reconciling over real HTTP to the loopback hook; item 9 approve-A/rev-B tests (claim+complete refused, A hash not re-approvable, late plan while running refused); item 18 injected-failure and SIGKILL-mid-migration subprocess tests (version unchanged, integrity ok, re-migrate succeeds); vacuous asserts removed (workflow item3, dispatch accepted-in-(0,1) and HttpResult tautology, MCP "sk-" check replaced by planted secrets in hooks/env/error inputs checked across MCP/stderr/status/hook list/export); full task/job/dispatch status x status matrix vs an independent expected table.
  - Results: doctor exit 0 (NOT OPERATIONAL banner, hooks unset); unit 109 passed exit 0; integration 45 passed exit 0; pyflakes exit 0; manual shadow-attack repro: rejected before running; isolation-only layer runs the real tests (exit 1); old argv without -I exit 0 'VERIFIED' (the original bypass).
  - Still NOT done: live items 20-22 not run; Bots/routines/hooks unverified; approval SOFT only; MCP not registered (T08); no Auto Review rule; shadow names derived from the host interpreter.
- 2026-11-03 21:33 MST: Fixes committed b7cdc15 (unit 109, integration 45, pyflakes clean). Re-review requested from Momus.
- 2026-11-03 21:37 MST: Momus re-review FAIL on b7cdc15 (verifier bypasses: committed .pyc, import-time os._exit(0), monkeypatched assertEqual). Round-2 fixes dispatched to build worker.
- 2026-11-03 21:38 MST: T08 partial — user approved; 'omo' MCP registered via AddMcpServer (server id user-omo, stdio, 8 tools, connected). Hephaestus live readback: omo.status OK (schema_version 2, 0 jobs, 8 unbound roles). Second-bot readback pending (ask Momus in next review).
- 2026-11-03 21:45 MST: Round-2 fixes for the Momus FAIL of b7cdc15, built and tested:
  - **G (planted .pyc).** Candidate diffs may contain only `source_extensions` (default .py). The verifier rejects
    `__pycache__`, .pyc/.pyo/.so, symlinks, submodules, binary files and non-UTF-8 files. Before the run it purges
    bytecode in the private checkout and runs with -B, PYTHONDONTWRITEBYTECODE and `-X pycache_prefix=<private dir>`.
  - **D (os._exit).** Added a trusted runner (omo_grok/trusted_runner.py, run with -I, outside the candidate tree).
    It runs the profile's expected_tests manifest and writes a JSON result outside the tree, bound to a per-run nonce
    passed by inherited fd. A pass needs all of: exit 0, the result file, the right nonce, every expected test passed,
    exact counts and the type checks.
  - **E (monkeypatched asserts).**
    - The runner snapshots and re-checks TestCase/TestResult/TestSuite/TestLoader attributes, builtins, key stdlib
      functions, sys.modules entries and import hooks after import and after the run.
    - A new static AST scan (omo_grok/candidate_scan.py) covers changed editable .py files. It rejects os._exit,
      sys.exit, the unittest/builtins/importlib/ctypes/gc/inspect/sys imports, sys.modules, frame and introspection
      attributes, setattr and module mutation, exec/eval/compile/__import__ and open. It is configurable per profile
      and cannot be disabled.
  - **F (int subclass with an always-True __eq__).** Mitigated, not solved.
    - The scan rejects builtin subclasses, metaclasses and dunder overrides. The runner type_checks require
      `type(r) is int`.
    - SECURITY.md states that a test-based verifier cannot in general defeat adversarial in-process code. Code
      review and QA bound to the same revision are the backstop.
  - **Profiles.** Python profiles without the trusted runner are refused. Non-Python profiles need
    `exit_code_only: true` and are labelled REDUCED assurance.
  - **Shadow names.** Snapshotted at install into config/shadow_names.json.
  - **Engine.**
    - A plan-bound task with NULL plan_revision_id fails closed at claim, checkpoint, complete, the packet builder
      and task creation.
    - submit rejects an unknown verifier_profile with ValidationError.
    - db.restore raises IntegrityError_ for corrupt backups.
  - **Tests.**
    - G, D (import and call), E (direct and setattr) and F each have three checks:
      - proven real: the old exit-code-only run gives exit 0
      - fails the full verifier, rejected statically
      - fails the runtime layer alone, with static checks disabled
    - test_without_isolation_the_attack_would_pass now actually runs the attack. Forged and nonce-less results,
      missing tests and non-source, symlink and binary files are tested.
    - Added 33 candidate_scan cases and approval-gate tests for code_review and qa claims (revoked approval, changed
      plan, NULL revision).
    - Narrowed raises to Conflict and IntegrityError_.
    - The MCP ERR_SECRET is now pattern-free, and the exact field-only error shape is asserted for status, claim and
      context.
  - **Results.** doctor exit 0 (NOT OPERATIONAL banner); unit 168 passed exit 0; integration 54 passed exit 0;
    pyflakes exit 0. The manual repro of G/D/E/F: all FAILED under the current verifier, and the real-fix control
    PASSED.
  - **Still NOT done / residual.**
    - Live items 20-22 not run; Bots/routines/hooks unverified; approval SOFT only; no Auto Review rule.
    - The static scan is a tripwire, not a sandbox. In-process code could in principle find the nonce in memory.
      F is undefeatable in general, so review and QA are the backstop.
    - The already-registered `user-omo` MCP server process must be restarted to load this code.
