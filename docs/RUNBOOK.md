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

Pasted values are cleaned before validation: bracketed-paste markers (`ESC[200~`/`ESC[201~`, with or without ESC),
every control or non-printable character (CR, LF, TAB, ESC, zero-width), surrounding whitespace, and one matching
pair of surrounding quotes or `<...>`. Each prompt allows 3 tries. A rejection prints only a diagnostic (length,
empty, scheme http/https/empty/other, host present, whether characters were removed), never the value.
Metis serves two roles (`metis`, then `qa-executor`) and Momus two (`momus`, then `code-reviewer`); each has its
own routine (`omo-bus-<role>`) with its own URL and key, so run `hook set` once per role.

**Browser form (when terminal paste delivers nothing):** some GUI paste tools cannot type into a terminal
(`hook set` then sees length=0). Use the local-only masked form instead:

```bash
cd /workspace/omo && nohup ./omoctl hook serve > /tmp/omo_hook_serve.log 2>&1 &
cat /tmp/omo_hook_form_url     # http://127.0.0.1:<random port>/setup/<one-time token>  (file mode 0600)
```

Open that URL in the box browser. There is one section per async role, in this order: explorer, librarian, metis,
qa-executor (Metis), momus, code-reviewer (Momus), executor. Each section shows the bot, the routine and a
"Configured already: yes/no" line, plus two masked fields (URL, key). Paste into the fields, leave both blank to
skip a role, then press **Save**. The page reports configured / skipped / error per role, with the same safe
diagnostics as `hook set` and never the values. You can resubmit as often as needed. **Finish** stops the server
and deletes the URL file. It also exits by itself after 30 idle minutes (`--idle-minutes N`).
How it is protected:
- It binds to 127.0.0.1 only, on a random port (`--port`).
- Every path except `/setup/<token>` returns 404.
- A Host other than `127.0.0.1:<port>`, or a foreign Origin, gets 403 (DNS-rebinding guard).
- Values travel only in the POST body (urlencoded, max 64 KB).
- Request logging is off, and responses are no-store/no-referrer with a strict CSP.
- Values pass through the same `sanitize_input` and `set_hook` validation as `hook set`, and are stored only
  in `state/secrets/hooks.json` (0600).

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
| `argv` | absolute interpreter, no shell; a Python interpreter must be run isolated (`-I` before `-m`). It must resolve (realpath) inside `/usr` (not `/usr/local`): children run the system interpreter inside the sandbox, never the venv |
| `cwd` | inside the worktree |
| `env_allow` | allowed environment variables |
| `timeout_seconds` | run time limit |
| `protected_paths` | paths the Executor must not change relative to base |
| `editable_paths` | allowlist: the ONLY paths the Executor may add, change, delete or rename |
| `runner` | `"omo-unittest-child"`, required for Python profiles; argv must then end with the absolute path of `omo_grok/unittest_child.py` (no `-m`) |
| `start_dir` | relative test discovery dir for the advisory unittest child (e.g. `tests`) |
| `expected_tests` | non-empty unique list of test ids that must ALL run and pass (no extras, skips or failures) |
| `oracle` | `{"name": "<key in omo_grok/oracles.py ORACLES>", "cases_per_function": 8..2000, "modules": [...]}`. **Required** for every profile named `fixture-*`. Without it a profile is `cooperative` (see below). `type_checks` was removed in T07 round 3 and is refused |
| `source_extensions` | allowlist of file extensions the candidate may add or change (default `[".py"]`) |
| `pure_modules` | `{"allow_imports": [...]}`. **Required** for every `fixture-*` profile: every changed `.py` under `editable_paths` must pass the pure-function rule (no environment/context observation, no state between calls; imports only from this list, at most `math`, `operator`, `cmath`, `numbers`). `fixture-calc-unittest` allows none |
| `static_scan` | AST-scan rule overrides (`allow_imports`, `allow_attrs`, `allow_calls`, `forbid_imports`, `allow_dunder_overrides`, `allow_builtin_subclass`); cannot be disabled |
| `exit_code_only` | required `true` for non-Python argv; the pass then rests on exit code alone (REDUCED assurance) |

A profile whose argv or referenced script contains `OMO_VERIFY_TEMPLATE` is refused. So are:
- a missing profile
- a Python profile without `-I` or without the unittest child and `expected_tests`, or a `fixture-*` profile without an `oracle` or `pure_modules`
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

The trusted parent (which never imports candidate code) then decides the result:
- **oracle profiles** (assurance `oracle`): in each of 3 varied, neutral sandboxed runs (with different
  address-space limits: low / 4 GiB / none) the oracle child must answer every case (exhaustive [-16,16]^2 grid,
  boundary pairs, keyword/mixed-form and aliased-equal-argument copies, bool cases, magnitudes up to 2**4096, fresh random draws and per-run fresh large-magnitude draws (50 per function per run); keyword
  calls make the parameter names `a`, `b` part of the contract) with a raw JSON value that is `type(v) is int` and equal to the trusted oracle's answer,
  then exit 0, and the runs must agree. The advisory unittest child must also report a clean run; its report alone
  does not make a run pass.
- **profiles without an oracle** (assurance `cooperative`): the unittest child's report decides, but a candidate can
  forge it. The detail line says so, the record stores `assurance=cooperative`, and the final gate requires an accepted
  `code_review` and `qa` on the same revision from Bots outside the executor's Bot and independence group (or an
  approval with `accept_reduced_assurance=true`, labelled REDUCED ASSURANCE).
- exit-code-only profiles are `cooperative` too.

See SECURITY.md for what this does and does not prove.

Every child that runs candidate code (unittest child, oracle runs, exit-code commands) runs inside the bubblewrap
sandbox (read-only `/usr`, fresh writable checkout copy, private `/tmp`, no network, no view of `state/`, the code,
the venv, `/home` or `/workspace`) with a scrubbed environment (no `OMO_*`, nothing secret in env, argv or fds). If
bwrap is unavailable verification fails closed (`no sandbox available`); `doctor` shows `candidate_sandbox`. Trusted
files (code, config, venv) are hashed before and after; any change fails the run with `TAMPER` and a `tamper` event.
Migration v3 adds `verifications.assurance`, v4 adds `verifications.sandbox` and `tamper_json` (auto-applied with a
backup on the next open). A missing base revision fails closed.
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
