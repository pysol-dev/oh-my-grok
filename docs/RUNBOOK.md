# omo-grok runbook

All paths are absolute. `OMO=/workspace/omo`. Everyday use is talking to Hephaestus (`agents/hephaestus-omo.md`).
The commands below are operator controls. Exit codes:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | internal |
| 2 | invalid |
| 3 | not found |
| 4 | conflict |
| 5 | gate refused |
| 6 | integrity |
| 7 | aborted / no TTY |
| 8 | doctor problems |
| 9 | test failed / not run |

## 1. Install (already done on this box)
```bash
cd /workspace/omo
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
./omoctl migrate
./omoctl doctor
./omoctl test unit
./omoctl test integration
```

## 2. Register the MCP server in Grok Bot (command server; user/parent step)
Register a custom **command** (stdio) MCP server named `omo`:

```
/workspace/omo/.venv/bin/python /workspace/omo/omo_grok/mcp_server.py
```

This command is also in `config/mcp-command.txt`. It needs no environment variables or secrets. Registration
applies account-wide.

Confirm it from **at least two Bots**, for example Hephaestus and Explorer: the tool list should contain exactly
`submit, claim, context, checkpoint, complete, status, reconcile, cancel`, and a `status` call should succeed. A local
`python -m` test does not count as registration.

## 3. Bind roles to Bots and routines
1. `./omoctl roles import config/roles.example.json` imports all 8 bindings as **unverified**, with no routine. The
   Bot ids come from the task brief and are unverified until read back.
2. On each Bot, create a webhook-triggered routine named `omo-bus-<role>`. Paste the instructions from
   `routines/<role>.md`. One Bot may host several role routines; the defaults are code-reviewer on Momus and
   qa-executor on Metis. The Hephaestus routine (`routines/hephaestus.md`) is optional.
3. Record the routine id and verify:
   `./omoctl roles bind ROLE --bot-id ID --bot-name NAME --routine-id RID --routine-name omo-bus-ROLE --group g-NAME --verified --provenance "read back <date>"`
   Use `--max-concurrent 1` for Bots that host several roles. Re-binding creates a new version; the old one is kept.
4. Optional: `routines/omo-reconcile.md` is a Hephaestus scheduled routine (every 15 min). Create it only if the
   user approves the ongoing usage.

## 4. Webhook secrets (user-only step)
For each async role, open the routine in the Grok Bot UI (desktop) and copy its webhook URL and key. Then run:

```bash
/workspace/omo/omoctl hook set ROLE   # hidden prompts for URL and key; nothing echoed
/workspace/omo/omoctl hook list       # role: configured true/false only
/workspace/omo/omoctl hook check ROLE --probe   # TLS handshake only, no POST
```

Never paste keys into chat, argv or files.

**Rotation:** rotate the key in the routine UI, then run `hook set ROLE` again, then
`resume JOB` for any job blocked with `webhook key rejected`.

## 5. Start a job
- Normally you say "plan this: <goal> in <repo>" to Hephaestus.
- Fallback: `./omoctl submit --goal-file goal.md --repo /path/to/repo [--plan-only|--implement|--fixture] [--criteria-file c.txt] [--verifier-profile NAME] [--idempotency-key K]`
- Real repos default to plan-only and must be clean git repos (optionally restricted by `repo_allowlist`).

## 6. Plan review and approval
- `./omoctl plan show JOB` prints the revision, hash, Momus verdict and full plan.
- `./omoctl approve JOB --plan-hash <64-hex> [--no-edit] [--accept-reduced-assurance]` needs an interactive TTY. You
  type the first 12 characters of the hash. It records a SOFT receipt.
- `./omoctl deny JOB --reason "..."` creates a new plan revision. The number of rounds is capped.

## 7. Pending work and status
- `./omoctl status [JOB] [--json]` shows the phase, `next_action`, task states, `block_reason`, dispatch state,
  verifications, gates and the `awaiting_resume` list.
- `awaiting_resume` tasks are Hephaestus tasks. Hephaestus claims them in its next chat turn.

## 8. Interrupted runs and reconciliation
`./omoctl reconcile [JOB]` (also the MCP tool `reconcile`) does the following:

- expires leases (requeue, or block when out of attempts)
- recovers interrupted sends as uncertain retries
- re-wakes unclaimed tasks after `rewake_after_seconds`, at most `max_rewakes` times, then blocks
- re-runs verification that stopped mid-run
- schedules ready work and flushes the outbox

It is safe to run repeatedly.

## 9. Verifier profiles
These live in `config/verifiers.json`. Each profile has these fields:

| Field | Requirement |
|---|---|
| `argv` | absolute interpreter, no shell; a Python interpreter must be run isolated (`-I` before `-m`) |
| `cwd` | inside the worktree |
| `env_allow` | allowed environment variables |
| `timeout_seconds` | run time limit |
| `protected_paths` | paths the Executor must not change relative to base |
| `editable_paths` | allowlist: the ONLY paths the Executor may add, change, delete or rename |
| `runner` | `"omo-trusted-unittest"`, required for Python profiles; argv must then end with the absolute path of `omo_grok/trusted_runner.py` (no `-m`) |
| `start_dir` | relative test discovery dir for the trusted runner (e.g. `tests`) |
| `expected_tests` | non-empty unique list of test ids that must ALL run and pass (no extras, skips or failures) |
| `type_checks` | optional `[{"call": "pkg.func", "args": [...], "type": "int", "expect": 5}]`, checked with `type(r) is T` and an exact value |
| `source_extensions` | allowlist of file extensions the candidate may add or change (default `[".py"]`) |
| `static_scan` | AST-scan rule overrides (`allow_imports`, `allow_attrs`, `allow_calls`, `forbid_imports`, `allow_dunder_overrides`, `allow_builtin_subclass`); cannot be disabled |
| `exit_code_only` | required `true` for non-Python argv; the pass then rests on exit code alone (REDUCED assurance) |

A profile whose argv or referenced script contains `OMO_VERIFY_TEMPLATE` is refused. So are:
- a missing profile
- a Python profile without `-I` or without the trusted runner and `expected_tests`
- a non-Python profile without `exit_code_only: true`

Before running, the verifier diffs base..candidate (`--no-renames`, so renames count as delete+add) and rejects the
candidate without executing it when any changed path:
- is outside `editable_paths` or inside `protected_paths`;
- is an interpreter or test-runner hook (`sitecustomize.py`, `usercustomize.py`, `*.pth`, `conftest.py`, `pytest.ini`,
  `tox.ini`, `setup.cfg`, `pyproject.toml`), at any depth;
- is a top-level module or package (in the cwd or an argv directory) whose name shadows a stdlib module, a module the
  verifier uses (`unittest`, `pytest`, `site`, `importlib`, ...) or anything in the interpreter's site-packages.
  The list is unioned with the install-time snapshot `config/shadow_names.json`, so a later interpreter change can
  only add names, never drop them. To regenerate it, write `sys.stdlib_module_names` with the venv Python.
- is a `__pycache__`/`.pyc`/`.pyo`/native file, a symlink, a submodule, has an extension outside `source_extensions`,
  is binary or not UTF-8, or (for `.py`) fails the static AST scan (see SECURITY.md).

The trusted runner then decides the result:
- exit 0
- the nonce-bound result file outside the checkout
- every expected test passed, with exact counts
- no tamper
- every type check ok

See SECURITY.md for what this does and does not prove.

The run itself uses `-I` plus `PYTHONSAFEPATH=1` and `PYTHONNOUSERSITE=1`, so even an unrecognised shadow cannot replace
the stdlib runner. A missing base revision fails closed.
`./omoctl doctor` lists the usable profiles.

## 10. Backups and migrations
- `./omoctl backup --out /path/file.sqlite3` uses the SQLite backup API and an integrity check.
- `./omoctl migrate --dry-run`, then `./omoctl migrate`. Migrate takes an automatic backup first when the DB is not
  empty. Migrations are versioned and checksummed.
- `./omoctl restore FILE` needs a TTY. Stop all omo activity first. It verifies the backup's integrity before
  swapping it in, and keeps the old DB.

## 11. Failed / dead-letter dispatches
- `status` shows the dispatch `status`, `http_status` and `last_error` (redacted).
- `failed` rows are permanent: 401/403/404/410, or retries exhausted.
- Fix the cause, then run `./omoctl resume JOB` (add `--grant-repair-cycles N` when the repair budget is exhausted).
  Then run `./omoctl reconcile JOB`.

## 12. Cancel and uninstall
- `./omoctl cancel JOB --reason "..."` asks for TTY confirmation. Nothing is deleted. Worktrees and branches in user
  repos are kept for inspection. Remove them yourself with
  `git worktree remove <path>` and `git branch -D omo/<id>` only when you want to.
- Uninstall:
  1. Remove the `omo` MCP registration.
  2. Disable or delete the `omo-bus-*` and `omo-reconcile` routines in the Grok Bot UI.
  3. Optionally run `omoctl export` for jobs you want to keep.
  4. Delete `/workspace/omo`.

  User repositories are never touched by the uninstall. Check that no worktree you need lives under `jobs/`
  (fixtures only).

## Troubleshooting
| Symptom | Cause / action |
|---|---|
| Webhook returned `200` but the task was never claimed | 200 only means accepted. Check that the routine run started and that the Bot has the `omo` MCP. `reconcile` re-wakes after `rewake_after_seconds`, at most `max_rewakes` times, then blocks with "never claimed". Check the routine instructions, then `resume` |
| Missing MCP (`omo.claim` unknown in the routine) | Register the command server (§2) and confirm it in that Bot. `./omoctl doctor` checks the server locally |
| Paused or deleted routine | HTTP 404/410 means "routine missing, paused or disabled". The task blocks with no retries. Re-enable the routine, re-bind if its id changed, then `resume` |
| Changed or wrong key | HTTP 401/403 means "webhook key rejected". Run `hook set ROLE`, then `resume` |
| Exhausted retry budget | The dispatch becomes `failed` after `dispatch_max_retries`. Repeated 429 means the platform rate limit: wait, then `resume`. Lower `per_bot_hourly_dispatch_limit` if needed |
| Repair budget exhausted (3 cycles) | The job is `blocked` with each cycle's diagnostics. `resume JOB --grant-repair-cycles 1` only after reading why |
| `dispatch deferred: no enabled role binding` / `bot_busy` | Bind or verify the role, or wait for the shared Bot to finish (per-Bot serialization) |
| Doctor shows "NOT OPERATIONAL" | Expected until `omoctl test live --fixture-only` passes with real bindings and hooks |
