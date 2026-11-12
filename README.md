# oh-my-grok

**oh-my-grok is not OmO.** It is a separate, durable workflow bus built for **Grok Bot**. It borrows OmO-style *role names and sequencing* (research → plan → approve → implement → verify → review → gate), but it is a different architecture: a SQLite outbox that wakes existing Grok Bots over HTTPS webhooks, not an in-process Codex/OpenCode plugin.

## Names

This tree is **oh-my-grok** (short name **omg**).

- Operator CLI: `omg`. `omoctl` is a thin shim that execs `omg`, so existing scripts and the frozen spec still have a command.
- Python package and import: `oh_my_grok`.
- MCP stdio server name in this tree: `omg`. The live Grok Bot connector is still `user-omo`. This change does not re-register it. The intended connector name is `omg` / `user-omg`.
- Routine files here use `omg-bus-<role>` and `omg-reconcile`. Live Grok Bot routines still have the old names until they are renamed in place at cutover. Do not delete them.
- Documented checkout: `/workspace/omg`. The live checkout is still `/workspace/omo` until that directory is moved.
- Environment variables are `OMG_*`. The previous `OMO_*` names are still accepted.
- A verifier profile whose argv still ends at `<code root>/omo_grok/unittest_child.py` (the pre-rename trusted child, including `${OMO_CODE_ROOT}/omo_grok/unittest_child.py`) is loaded as `oh_my_grok/unittest_child.py`. Other paths are not rewritten. Runner name `omo-unittest-child` is still accepted. `OMO_VERIFY_TEMPLATE` still fails closed.
- New databases are `state/omg.sqlite3`. If that file is absent and `state/omo.sqlite3` exists, the engine opens the existing file.
- Boulder files are written with `x_omg`. Readers also accept legacy `x_omo_grok`. The directory `.omo/` is the upstream oh-my-openagent boulder path and is unchanged.
- `spec/IMPLEMENTATION.md` was **not** updated. It still says omo-grok, omoctl, omo_grok, and `/workspace/omo`. Its SHA-256 is still `9643ee06f6817b08c57ea585fd8f919d4b23639bffd817a4a3084c7c9d0d30ae`. Newer docs use oh-my-grok.
- The live GitHub repository is [https://github.com/pysol-dev/omo-grok](https://github.com/pysol-dev/omo-grok). The intended name is `pysol-dev/oh-my-grok`. That URL is not live yet. After the repository is renamed, the old URL will redirect.

## Status

**PARTIALLY INSTALLED / NOT OPERATIONAL.**

- Code, the live MCP connector (`user-omo`, not yet re-registered), bindings, hooks, unit/integration suites, and live fixture jobs pass on **DB evidence**. Momus round 13 signed off on `028255b`.
- This branch adds adaptive orchestration (intent gating, roster, boulder projection, real language-server gates, Context7 receipts, git-history checks). Every new behaviour defaults to **off**. Implementing it here does not install it on the host and does not make the installation OPERATIONAL.
- Failure collection (`omg heal`, MCP `heal`) is also off until `features.healing` is `shadow` or `enforce`. Collect writes a snapshot of failures already in the database. It does not enqueue a fix. See `spec/HEALING.md`.
- `./omg doctor` derives its live-status line from recorded evidence (a passed real-repo gate with engine LSP and git receipts, plus human attestations of host cutover and the original live smoke and end-to-end items). With none of those, it reports NOT OPERATIONAL.
- Routine run **receipts** from the Grok Bot UI are still not readable from this session, so this install is not called OPERATIONAL.
- Webhook keys were entered in chat (user choice on 2026-11-04). **Keys are not being rotated** (user request). Rotation stays optional until a chat paste path that keeps secrets out of history exists. See `docs/SECURITY.md`.

## What it does (short)

Talk to **Hephaestus** in natural language. Hephaestus submits a job over the `omg` MCP. Then:

1. Explorer and Librarian research in parallel.
2. Metis analyses the gaps.
3. Hephaestus drafts the plan (in chat / MCP; not a self-webhook).
4. Momus reviews the plan.
5. **You approve the exact plan hash** (SOFT TTY receipt today).
6. The Executor works in an isolated git worktree.
7. A deterministic verifier (bubblewrap + oracle for fixture profiles) checks the result.
8. Code Reviewer and QA review that exact revision.
9. A revision-bound final gate closes the job.

Eight *logical* roles map onto five existing Grok Bots (Momus also runs code-reviewer; Metis also runs qa-executor). Shared-Bot independence is disclosed as `separate_bots_same_account` (reduced vs separate accounts).

## How this differs from OmO

Upstream OmO is the **oh-my-openagent / LazyCodex** lineage:

- Plugin / docs: [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent), [LazyCodex docs](https://lazycodex.ai/docs)

| Topic | Upstream OmO (oh-my-openagent / LazyCodex) | oh-my-grok (this repo) |
|---|---|---|
| Where it runs | In-process Codex / OpenCode **plugin** harness | Separate process on the shared Bot computer under `/workspace/omg` |
| Continuity | Session lifecycle hooks (`SessionStart`, `PreToolUse`, `Stop`, …); boulder state in `.omo/boulder.json`; plans under `.omo/plans/` | Durable **SQLite** job/task/outbox DB (`state/omg.sqlite3`, or an existing `state/omo.sqlite3`) is the only authority. With `boulder=on`, `.omo/boulder.json` and an active-plan markdown file are a locked projection of that DB and are never read back |
| How work is delegated | Native multi-agent / `spawn_agent` inside one Codex session family; model routing per role | HTTPS **webhook outbox** wakes Grok Bot routines (`omg-bus-*`); body is **ids-only** + Bearer key |
| Primary agents | Sisyphus / Hephaestus / Prometheus / Atlas (+ explore, librarian, metis, momus, …) | Eight logical roles on existing Grok Bots; **no** Sisyphus, Atlas, Prometheus, or ulw hooks |
| Intake | Same-machine session continuity | Hephaestus owns the goal in **chat** → MCP `submit`; optional resume webhook draft exists, not required for intake |
| Approval | Plan / work loops inside the plugin session | **Plan-hash SOFT** approval via `omg approve` (TTY); strict external signatures documented, not implemented |
| Verification | Ultralwork / ulw-loop / start-work hooks; comment-checker, LSP, rules injection | bubblewrap sandbox + trusted **oracle** for fixture profiles; with `lsp=enforce`, a base-versus-candidate delta from real language servers (ruff, basedpyright, typescript-language-server). Other languages fail closed. Momus/Metis as separate bots (same account → independence disclosed as reduced) |
| Install shape | Codex plugin install | MCP stdio server `omg` + `omg` CLI (`omoctl` shim); **not** a native OmO hook implementation |

**In one sentence:** OmO keeps a multi-agent loop *inside* one Codex session with plugin hooks and boulder state; oh-my-grok keeps durable job state in SQLite and *pings separate Grok Bots* when a task is ready.

Public repo, live today: [https://github.com/pysol-dev/omo-grok](https://github.com/pysol-dev/omo-grok) (`main`). The intended name `pysol-dev/oh-my-grok` is not live yet; the old URL will redirect after the repository is renamed. Secrets (`state/`, `hooks.json`, `.venv`) are gitignored and were never pushed.

## Quick start

```bash
cd /workspace/omg
./omg doctor
./omg test unit && ./omg test integration
./omg status
```

MCP command (stdio server `omg`; the live connector is still `user-omo` until it is re-registered):

```text
/workspace/omg/.venv/bin/python /workspace/omg/oh_my_grok/mcp_server.py
```

Day-to-day: ask Hephaestus in chat (*“Use omg to plan a fix for …”*). CLI fallbacks: `./omg status`, `./omg plan show <job-id>`, `./omg approve <job-id> --plan-hash <hash>`, `./omg reconcile`, `./omg doctor`. `./omoctl` runs the same CLI.

## Docs

| Doc | What |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Engine, data model, lifecycle, dispatch |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model, soft vs strict approval, keys, residuals |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operator steps, hooks, troubleshooting |
| [`docs/MIGRATION.md`](docs/MIGRATION.md) | Schema versions, cutover, rollback |
| [`spec/PROGRESS.md`](spec/PROGRESS.md) | Authoritative implementation ledger |
| [`spec/IMPLEMENTATION.md`](spec/IMPLEMENTATION.md) | Frozen original spec. Still uses the old names. Hash must not change |

## Layout

| Path | What |
|---|---|
| `omg` | Operator CLI wrapper (pinned `.venv`). `omoctl` execs this script |
| `oh_my_grok/` | Engine, MCP server, CLI, DB/migrations, dispatcher, artifacts, verifier, git worktrees, redaction |
| `config/` | `verifiers.json`, `bots.json` (unverified Bot ids), `roles.example.json`, optional `policy.json` |
| `routines/` | Instructions for each `omg-bus-<role>` routine, optional Hephaestus resume (`hephaestus.md`), and `omg-reconcile.md` |
| `agents/hephaestus-omg.md` | Natural-language conventions for Hephaestus |
| `docs/` | Architecture, security, runbook, migration |
| `tests/unit`, `tests/integration` | Automated tests for spec §10 items 1–19 (network mocked or loopback) |
| `fixtures/` | Synthetic `calc` fixture source |
| `spec/` | Spec (read-only) and ledger |
| `state/`, `jobs/` | Runtime DB, secrets (`state/secrets/hooks.json`, gitignored, mode 0600), logs, job artifacts / worktrees |

Approval today is **soft**: a local TTY receipt on a shared computer. See `docs/SECURITY.md`.
