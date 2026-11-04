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
- There is no live cutover yet. Nothing is registered or bound live (ledger T08–T11).
- Rollback:
  1. Remove the MCP registration.
  2. Disable the `omo-bus-*` routines.
  3. Leave the state DB in place for evidence.
