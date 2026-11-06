# Migration notes

## Prior build
Reconnaissance on 2026-11-03 (ledger T00/T01) found that `/workspace/omo` **did not exist** before this run. None of
the following existed anywhere on the box:

- `bus.py` or `omoctl`
- an omo SQLite database
- `hooks.json`
- any omo routine

The earlier claim of an existing SQLite build at `/workspace/omo` could therefore not be reproduced or backed up. It
is recorded as an **unverified prior claim**. Nothing was migrated, and existing Bots and routines were not touched.

## Schema migrations in this build
| Version | Content |
|---|---|
| 1 | Core tables: jobs, tasks, deps, attempts, artifacts, inputs, plan revisions, approvals, role bindings, outbox, verifications, reviews, events |
| 2 | `hooks_meta` (booleans only) and `gate_results` tables plus indexes. See `omo_grok/db.py` `MIGRATIONS` |
| 3 | `verifications.assurance TEXT NOT NULL DEFAULT 'none'` (oracle / cooperative / none). Applied automatically on the next open, after the automatic backup |
| 4 | `verifications.sandbox TEXT NOT NULL DEFAULT 'none'` (mechanism used: `bwrap` / `none`) and `verifications.tamper_json TEXT NOT NULL DEFAULT '[]'` (trusted-file changes seen during the run). Old rows read `none` / `[]` |

Each migration is recorded in `schema_migrations` with a checksum, and a mismatch is reported by `doctor`.
`omoctl migrate` takes an automatic backup first when the DB is not empty. Roll back with `omoctl restore <backup>`.

## Cutover and rollback

### What this installation is
This build **is** the installation under `/workspace/omo`. There is nothing older to retire here: recon (T00/T01)
found no prior `bus.py`, SQLite DB, hooks, or omo routines on the box. Prior claims of an existing SQLite build at
this path were unreproducible (see Prior build above).

### What exists now (as of 2026-11-06)
- SQLite schema **v4** at `state/omo.sqlite3` (migrations 1–4). Automatic pre-migrate backups live under
  `state/backups/pre-migrate-v*.sqlite3` (currently `pre-migrate-v2-*.sqlite3` and `pre-migrate-v3-*.sqlite3`).
- `user-omo` MCP registered (stdio command server).
- **8 logical roles** on **5 bots**: Hephaestus plus Explorer, Librarian, Metis, Momus, and Executor (Metis also
  binds `qa-executor`; Momus also binds `code-reviewer`).
- **7** `omo-bus-*` webhook routines created, verified, and hooked (explorer, librarian, metis, momus, executor,
  code-reviewer, qa-executor). The optional Hephaestus resume webhook is **not configured**.
- Live jobs on DB evidence: `e667cf20` (§10.20 / §10.21) and `9fa4f055` (§10.22). See ledger T11.
- Status remains **PARTIALLY INSTALLED / NOT OPERATIONAL**. Do not claim OPERATIONAL.

### Cutover meaning
Cutover means accepting this tree as the live install and continuing operations against it — not migrating off a
previous `/workspace/omo` build. Bots and routines already created stay in place.

### Rollback (code / DB)
1. Stop omo activity (no in-flight claims). After any code or DB rollback, restart/reload the `user-omo` MCP so
   processes are not stale (fingerprint / `stale_server` check).
2. Restore SQLite from a backup under `state/backups/` with
   `./omoctl restore state/backups/pre-migrate-vN-<timestamp>.sqlite3` (TTY required; integrity-checked; keeps a
   pre-restore copy of the current DB).
3. Keep `state/secrets/hooks.json` mode **0600** (`doctor` checks this). Do not make it world-readable.
4. **Do not delete** Bots or `omo-bus-*` / related routines as part of a rollback — leave them configured.
5. Grok Bot conversations and agent memory are **not** restored from filesystem backups; only the SQLite state
   (and whatever you keep under `state/` / `jobs/`) comes back from `omoctl restore` / file restore.

Uninstall (remove the MCP registration, disable/delete routines, delete the tree) is a separate destructive path in
`docs/RUNBOOK.md` §12 — not the rollback path.

### Honest gaps (still open)
- Routine run receipts for the live jobs are still pending (not readable from Hephaestus's session); T11 remains
  PARTIAL.
- Webhook key rotation is recommended (keys were chat-pasted on 2026-11-04; see `docs/SECURITY.md` and RUNBOOK
  rotation steps).
- Approval is **SOFT** only (local TTY receipt on a shared computer).
- No Auto Review rule was created.
- Hephaestus resume webhook is optional and not set (`hephaestus` remains unhooked).
