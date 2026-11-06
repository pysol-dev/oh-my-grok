# omo-grok

**omo-grok is not OmO.** It is a separate, durable workflow bus built for **Grok Bot**. It borrows OmO-style *role names and sequencing* (research → plan → approve → implement → verify → review → gate), but it is a different architecture: a SQLite outbox that wakes existing Grok Bots over HTTPS webhooks, not an in-process Codex/OpenCode plugin.

## Status

**PARTIALLY INSTALLED / NOT OPERATIONAL.**

- Code, MCP (`user-omo`), bindings, hooks, unit/integration suites, and live fixture jobs pass on **DB evidence**. Momus round 13 signed off on `028255b`.
- `./omoctl doctor` still prints the NOT OPERATIONAL banner.
- Routine run **receipts** from the Grok Bot UI are still not readable from this session, so this install is not called OPERATIONAL.
- Webhook keys were entered in chat (user choice on 2026-11-04). **Keys are not being rotated** (user request). Rotation stays optional until a chat paste path that keeps secrets out of history exists. See `docs/SECURITY.md`.

## What it does (short)

Talk to **Hephaestus** in natural language. Hephaestus submits a job over the `omo` MCP. Then:

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

| Topic | Upstream OmO (oh-my-openagent / LazyCodex) | omo-grok (this repo) |
|---|---|---|
| Where it runs | In-process Codex / OpenCode **plugin** harness | Separate process on the shared Bot computer under `/workspace/omo` |
| Continuity | Session lifecycle hooks (`SessionStart`, `PreToolUse`, `Stop`, …); boulder state in `.omo/boulder.json`; plans under `.omo/plans/` | Durable **SQLite** job/task/outbox DB (`state/omo.sqlite3`); no `boulder.json` |
| How work is delegated | Native multi-agent / `spawn_agent` inside one Codex session family; model routing per role | HTTPS **webhook outbox** wakes Grok Bot routines (`omo-bus-*`); body is **ids-only** + Bearer key |
| Primary agents | Sisyphus / Hephaestus / Prometheus / Atlas (+ explore, librarian, metis, momus, …) | Eight logical roles on existing Grok Bots; **no** Sisyphus, Atlas, Prometheus, or ulw hooks |
| Intake | Same-machine session continuity | Hephaestus owns the goal in **chat** → MCP `submit`; optional resume webhook draft exists, not required for intake |
| Approval | Plan / work loops inside the plugin session | **Plan-hash SOFT** approval via `omoctl approve` (TTY); strict external signatures documented, not implemented |
| Verification | Ultralwork / ulw-loop / start-work hooks; comment-checker, LSP, rules injection | bubblewrap sandbox + trusted **oracle** for fixture profiles; Momus/Metis as separate bots (same account → independence disclosed as reduced) |
| Install shape | Codex plugin install | MCP stdio server `omo` + `omoctl` CLI; **not** a native OmO hook implementation |

**In one sentence:** OmO keeps a multi-agent loop *inside* one Codex session with plugin hooks and boulder state; omo-grok keeps durable job state in SQLite and *pings separate Grok Bots* when a task is ready.

Public repo: [https://github.com/pysol-dev/omo-grok](https://github.com/pysol-dev/omo-grok) (`main`). Secrets (`state/`, `hooks.json`, `.venv`) are gitignored and were never pushed.

## Quick start

```bash
cd /workspace/omo
./omoctl doctor
./omoctl test unit && ./omoctl test integration
./omoctl status
```

MCP command (stdio server `omo`):

```text
/workspace/omo/.venv/bin/python /workspace/omo/omo_grok/mcp_server.py
```

Day-to-day: ask Hephaestus in chat (*“Use omo to plan a fix for …”*). CLI fallbacks: `./omoctl status`, `./omoctl plan show <job-id>`, `./omoctl approve <job-id> --plan-hash <hash>`, `./omoctl reconcile`, `./omoctl doctor`.

## Docs

| Doc | What |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Engine, data model, lifecycle, dispatch |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model, soft vs strict approval, keys, residuals |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operator steps, hooks, troubleshooting |
| [`docs/MIGRATION.md`](docs/MIGRATION.md) | Schema versions, cutover, rollback |
| [`spec/PROGRESS.md`](spec/PROGRESS.md) | Authoritative implementation ledger |
| [`spec/IMPLEMENTATION.md`](spec/IMPLEMENTATION.md) | Spec (read-only; hash must not change) |

## Layout

| Path | What |
|---|---|
| `omoctl` | Operator CLI wrapper (pinned `.venv`) |
| `omo_grok/` | Engine, MCP server, CLI, DB/migrations, dispatcher, artifacts, verifier, git worktrees, redaction |
| `config/` | `verifiers.json`, `bots.json` (unverified Bot ids), `roles.example.json`, `mcp-command.txt`, optional `policy.json` |
| `routines/` | Instructions for each `omo-bus-<role>` routine, optional Hephaestus resume (`hephaestus.md`), and `omo-reconcile.md` |
| `agents/hephaestus-omo.md` | Natural-language conventions for Hephaestus |
| `docs/` | Architecture, security, runbook, migration |
| `tests/unit`, `tests/integration` | Automated tests for spec §10 items 1–19 (network mocked or loopback) |
| `fixtures/` | Synthetic `calc` fixture source |
| `spec/` | Spec (read-only) and ledger |
| `state/`, `jobs/` | Runtime DB, secrets (`state/secrets/hooks.json`, gitignored, mode 0600), logs, job artifacts / worktrees |

Approval today is **soft**: a local TTY receipt on a shared computer. See `docs/SECURITY.md`.
