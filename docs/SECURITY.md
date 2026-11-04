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
  paths. Missing or template (`OMO_VERIFY_TEMPLATE`) profiles fail closed. The final gate re-runs the verifier on the
  exact final revision.
- Logs, status, exports, errors and CLI output pass through `redact.py`. `hook list` shows booleans only.

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
