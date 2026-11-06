# omo-grok

omo-grok is a durable, SQLite-backed workflow bus. It lets one user drive an OmO-style multi-role software workflow
by talking to **Hephaestus**. The flow:

1. Explorer and Librarian research in parallel.
2. Metis analyses the gaps.
3. Hephaestus drafts the plan.
4. Momus reviews the plan.
5. **The user explicitly approves the exact plan hash.**
6. The Executor works in an isolated worktree.
7. A deterministic verifier checks the result.
8. Code Reviewer and QA review the exact revision.
9. A revision-bound final gate closes the job.

**Status: PARTIALLY INSTALLED / NOT OPERATIONAL.** The code and automated tests pass (`./omoctl doctor` still reports
NOT OPERATIONAL). The `omo` MCP is registered, all 7 `omo-bus-<role>` routines exist with webhook keys, and the live
tests passed on DB evidence on 2026-11-04: §10.20 (job e667cf20), §10.21 (same job, plus a submit via the user-omo MCP
from Hephaestus's session in 9fa4f055 — actor is the default role name, not an authenticated caller) and §10.22 (job
9fa4f055). Routine run receipts are pending (not readable from Hephaestus's session). Webhook key rotation is
recommended (see `docs/SECURITY.md`). See `spec/PROGRESS.md`.

## Quick start
```bash
cd /workspace/omo
./omoctl doctor
./omoctl test unit && ./omoctl test integration
./omoctl status
```

MCP command (stdio server `omo`): `/workspace/omo/.venv/bin/python /workspace/omo/omo_grok/mcp_server.py`

## Layout
| Path | What |
|---|---|
| `omoctl` | Operator CLI wrapper (pinned `.venv`) |
| `omo_grok/` | Engine, MCP server, CLI, DB/migrations, dispatcher, artifacts, verifier, git worktrees, redaction |
| `config/` | `verifiers.json`, `bots.json` (unverified Bot ids), `roles.example.json`, `mcp-command.txt`, optional `policy.json` |
| `routines/` | Instructions for each `omo-bus-<role>` routine, the optional Hephaestus resume routine, and `omo-reconcile` |
| `agents/hephaestus-omo.md` | Natural-language conventions for Hephaestus |
| `docs/` | `ARCHITECTURE`, `SECURITY` (threat model, soft vs strict approval), `RUNBOOK`, `MIGRATION` |
| `tests/unit`, `tests/integration` | Automated tests for spec §10 items 1–19. The network is mocked or uses loopback; concurrency tests use real subprocesses |
| `fixtures/` | Synthetic `calc` fixture source |
| `spec/` | The spec (read-only) and the implementation ledger |
| `state/`, `jobs/` | Runtime DB, secrets, logs and job artifacts or worktrees (git-ignored) |

Approval is **soft**: it is a local TTY receipt on a shared computer. See `docs/SECURITY.md`.
