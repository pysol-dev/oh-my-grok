# omo-grok security model

## Threat model (be honest about the boundary)
Every Grok Bot on this account shares one Linux computer. A same-account Bot can:

- use the shell and read every file it can write, including `state/omo.sqlite3`, `state/secrets/hooks.json`, the
  code and the config
- spoof an advisory role, run `omoctl` (including `approve` if it fakes a TTY), edit the DB, or change the verifier
- use any credential stored on the box

So the `omo` MCP server is a **robust cooperative state machine, not a sandbox**. Its role policy, schemas and
transition checks prevent *accidental* miscoordination: stale or duplicate wakeups, crossed jobs, unreviewed plans,
fabricated "PASS" text, and runaway retries. They do **not** stop a deliberately non-compliant Bot.

What the design does guarantee for cooperating agents:

- MCP exposes only `submit, claim, context, checkpoint, complete, status, reconcile, cancel`. There is no approve,
  waive, mark-done, verifier-off, shell or secret tool. `omoctl doctor` and the tests assert this.
- Webhook bodies are identifier packets with a fixed schema. They are never executed or interpreted as
  instructions. Claims need the exact current `dispatch_id`. Completions need the exact live `attempt_id` within
  its lease.
- Inputs are bounded and validated (pydantic on MCP, strict per-kind schemas, size limits). Artifacts are immutable,
  content-addressed and confined to the job's artifact directory: traversal and symlinks are rejected, and sha256 is
  re-verified on every read.
- The deterministic verifier runs argv lists without a shell, with an env allowlist, a timeout and protected test
  paths. Missing or template (`OMO_VERIFY_TEMPLATE`) profiles fail closed. Python runs isolated (`-I`,
  `PYTHONSAFEPATH`, `PYTHONNOUSERSITE`). Candidates are rejected before execution when they touch paths outside the
  profile's `editable_paths`, protected paths, interpreter/test hooks (`sitecustomize`, `usercustomize`, `.pth`,
  `conftest.py`, runner config) or add a top-level module that shadows a stdlib/verifier/site-packages name
  (regression: a committed `unittest/__init__.py` that printed a fake pass). Shadow names come from the host
  interpreter's `sys.stdlib_module_names` and site-packages listing. The final gate re-runs the verifier on the
  exact final revision.
- Verifier hardening after Momus re-review #2 (attacks G, D, E, F). Each has a regression test in
  `tests/unit/test_verifier.py`. That file also proves each attack is real: every one passes an exit-code-only run.
  - **Bytecode (G).** The candidate diff may contain only source files from the profile's `source_extensions`
    allowlist (default `.py`). It is rejected if it contains any of the following:
    - `__pycache__`, `.pyc`, `.pyo` or native-extension files
    - other non-source files, symlinks or submodules
    - files with NUL bytes or that are not UTF-8

    Separately, the private checkout has every `__pycache__` and `*.pyc` purged before the run. The run itself uses
    `-B`, `PYTHONDONTWRITEBYTECODE=1` and `-X pycache_prefix=<private dir>`, so a planted UNCHECKED_HASH `.pyc` is
    never loaded.
  - **Round 3 architecture (after Momus attacks N1–N4).** The round-2 design ran a "trusted runner" inside the same
    process as the candidate and trusted its nonce-bound result file. N1 (climbing traceback frames to the runner's
    nonce, `result_path` and `write_result`) and N2 (the same via `os.sys._getframe`) forged that file; N3 hid the N1
    payload behind a `# -*- coding: unicode_escape -*-` cookie; N4 read `OMO_VERIFY_RESULT`/`OMO_VERIFY_SPEC` from
    `/proc/self/environ`. The design was replaced, not patched:
    - **The parent verifier never imports candidate code.** Every candidate import happens in a child run with
      `-I -B -X pycache_prefix=<private dir>` and a scrubbed environment (PATH, LC_ALL, HOME, the bytecode/site
      switches and the profile's `env_allow`/`env_set`; no `OMO_*`). No nonce, result path or other secret is passed
      in env, argv or fds, so there is nothing for N1/N2/N4 to steal. Everything a child says is untrusted.
    - **ORACLE mode (assurance `oracle`).** `omo_grok/oracles.py` (trusted tree) generates fresh SystemRandom inputs
      for every run (magnitudes up to ±2**200, shuffled). `omo_grok/oracle_child.py` imports the candidate and must
      print `{"ready":true}`; the parent then sends **one** request at a time over stdin and generates the next only
      after the answer arrives. Each answer must be a raw JSON value; the parent checks `type(v) is int` (bool and
      float never pass) and equality with its own oracle value. The child must answer every case and exit 0. Exiting
      early, printing `VERIFIED`, forging stdout, always-equal objects and constant answers all fail, with or
      without the static scan (regression tests). An oracle is **required** for every `fixture-*` profile, at config
      load and at submit.
    - **Advisory unittest child.** `omo_grok/unittest_child.py` runs the `expected_tests` manifest in a separate
      child and prints one `OMO-UNITTEST-REPORT` line. It still snapshots test machinery, builtins and key stdlib
      functions as a tripwire (round-2 E). For oracle profiles the report is advisory: a missing or negative report
      fails the run, but a positive report can never make it pass.
    - **Cooperative assurance.** A Python profile without an oracle, and every `exit_code_only` profile, is
      `cooperative`: its pass is forgeable by a malicious candidate. The detail line starts
      `assurance=cooperative: ... forgeable`, every verification record stores `assurance`, and the final gate
      requires `verifier_assurance_recorded` plus an accepted `code_review` **and** `qa` on the same revision
      (`cooperative_verifier_backed_by_code_review_and_qa`). The gate's assurance string carries
      `+verifier_<level>`.
  - **Static AST scan of every changed editable `.py` file (tripwire, not a sandbox).** Fail closed: a file that does
    not parse is rejected. Sources are decoded with `tokenize.detect_encoding`, and any coding cookie other than
    UTF-8, or a BOM/cookie mismatch, is rejected (N3). The scan rejects:
    - `os._exit`, `sys.exit`, `SystemExit`, `exit`/`quit`
    - importing `unittest`, `builtins`, `importlib`, `ctypes`, `gc`, `inspect`, `sys`, `io`, `threading`,
      `subprocess`, ..., and from-imports of forbidden modules or attributes
    - `__builtins__`, `sys.modules`, frame and introspection attributes (`__globals__`, `__code__`, `__class__`,
      `__subclasses__`, `__traceback__`, `tb_frame`, `tb_next`, `f_back`, `f_locals`, `f_globals`, `f_trace`,
      `_getframe`, `_current_frames`, `settrace`, ...)
    - full attribute chains resolved through aliases: `os.sys._getframe` is "chained access to forbidden module
      os.sys" (N2)
    - `getattr`/`hasattr` with a non-literal name, or a literal forbidden/dunder name; any `setattr`/`delattr`
    - attribute or subscript assignment on imported modules, `exec`/`eval`/`compile`/`__import__`/`open`
    - `global`, star imports, metaclasses, subclassing builtins, overriding comparison, hash, conversion and
      attribute dunders

    Profiles may relax this with `static_scan` allow-lists or tighten it (`forbid_imports`). They cannot disable it.
  - **Residual risk, stated plainly.**
    - The oracle is black-box testing. A candidate that senses the oracle harness and behaves correctly only there
      cannot be detected by it. Code review and QA are the backstop.
    - The oracle covers only the functions defined in `oracles.py` (today `calc.add`, `calc.mul` for `calc-arith`).
    - Profiles for real repos without an oracle are `cooperative`: the verifier result is forgeable, and only the
      required `code_review` + `qa` on the same revision stand behind it.
    - The static scan is a tripwire and will not cover every construct.
- Logs, status, exports, errors and CLI output pass through `redact.py`. `hook list` shows booleans only.
  Dispatch errors are scrubbed of the webhook URL/key at write time and every http(s) URL in them is cut to
  `scheme://host` (also on display), so status never shows a webhook URL even in a process that never loaded the hooks
  file. Exports redact `lease_token` and `claim_token`. MCP argument-validation errors report field names only, never
  the rejected values.
- Inline artifacts are written to a content-addressed path (`task/aN/<sha16>/name`) with an atomic temp+link publish;
  the engine never deletes or overwrites an artifact file, so a rolled-back submission cannot remove a registered
  artifact and same-content retries are idempotent.
- Implementation-side tasks (implement, code_review, qa, integrate) can be claimed, checkpointed or completed only under
  a live approval of the job's current plan revision; revoking an approval cancels their unstarted tasks.

## Secrets
- Webhook URL and key pairs live only in `state/secrets/hooks.json` (mode 0600, directory 0700). They are entered
  through `omoctl hook set ROLE` using a hidden `getpass` prompt. They never go in argv, shell history, chat, README,
  commits or test logs.
- This file mode prevents **accidental disclosure** only. Any same-account Bot can still read it.
- `hook check --probe` performs a TLS handshake only. It never POSTs and never prints the URL path or the key.
- Never put login tokens, OAuth or session material, or webhook secrets in agent prompts, routine instructions,
  repository commits or routine outputs.
- Rotation: run `omoctl hook set ROLE` again with the new value. Rotate the key in the routine UI first. A changed key
  shows up as `HTTP 401/403: webhook key rejected` and blocks the task until resumed.

## Approvals: SOFT (implemented) vs STRICT (protocol only, not implemented)

### Soft/local (what this build provides)
`omoctl approve JOB --plan-hash H` checks all of the following:

- the exact job
- that `H` equals the canonical sha256 of the current plan revision
- that Momus accepted that revision
- that the requirements hash is unchanged
- that the repository and scope are within policy
- an interactive TTY confirmation: you type the first 12 characters of the hash

It then records a receipt with mode `soft`. A plan change, a requirements change or a `deny` revokes the approval.

Fixture jobs may record an explicit `synthetic_fixture` approval, labelled `+synthetic_approval`. It is never valid
for a real repository.

**This is not a hard trust boundary.** A Bot on the same computer can fake a TTY or write to the DB. Treat soft
approval as protection against mistakes and misreads, not against a malicious agent.

### Strict/external (documented protocol; NOT implemented, no authority configured)
1. The approval authority is a key held **off the shared computer**: a user device, a hardware key, or a trusted
   external service. No private key is ever generated or stored in `/workspace/omo`.
2. The engine emits an approval request: `{job_id, plan_hash, requirements_hash, repo, scope, nonce, expires_at}`.
3. The user signs that canonical JSON on their own device (for example with an Ed25519 key, or a minisign or
   ssh-keygen `-Y sign` namespace `omo-approval`).
4. The engine verifies the signature against a pinned public key. It also checks that the nonce is unused (replay
   protection) and that `expires_at` has not passed. It then records `mode=strict` together with the signature.
5. Even strict approval cannot protect an unrestricted, Bot-writable backend from direct tampering.
   **Production merge and deploy must be gated outside this computer**: protected branches, required reviews, and
   independently trusted CI with credentials that Bots do not hold.

Because no external authority is configured, the engine **fails closed** on production merge or deploy. Approval
scope always records `production_merge_or_deploy: false`, and omo-grok never pushes, merges or deploys.

## Auto Review recommendation (user action; not verified by this build)
In Grok Bot settings, add a personal Auto Review **Ask first** rule covering:

- edits under `/workspace/omo/config/`, `/workspace/omo/state/` and `/workspace/omo/omo_grok/`
- running `omoctl approve`, `omoctl hook set`, `omoctl restore` and `omoctl roles bind`
- enabling or creating routines
- `git push`

Such a rule is **not** a universal interceptor and **not** a security boundary: it can miss indirect routes. This
build did not create or verify any Auto Review rule.

## Other safeguards
- No root or sudo. No remote installers. Dependencies are pinned with hashes in `requirements.lock` and installed
  into `.venv`.
- No unofficial `grok-bot-cli` and no private gateway endpoints. Dispatch uses only the documented routine webhook.
- Git: repo hooks are disabled for engine git calls. The engine never runs reset, clean, force or push, and never
  deletes branches. Each job works in its own worktree and branch.
- Changes to orchestrator code, config or verifier policy should be reviewed (ledger T07: Momus review).

## External network dependencies
| Dependency | Failure behaviour |
|---|---|
| Grok Bot routine webhooks (HTTPS POST) | Retried with capped backoff. 4xx auth or missing-routine responses block with a diagnostic. Re-wakes are bounded |
| Grok Bot routine runs (platform usage, hourly limits) | An hourly per-Bot cap, per-Bot serialization and a bounded re-wake count keep usage low. Unclaimed work blocks; it does not flood |
| PyPI (installation only) | Install from `requirements.lock`. No network at runtime |
