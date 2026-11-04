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

**Status: PARTIALLY INSTALLED / NOT OPERATIONAL.** The code and the automated tests are done and pass. MCP
registration in Grok Bot, the routines and webhook keys, and the live cross-Bot tests (spec §10 items 20–22) have not
been done yet. See `spec/PROGRESS.md`.

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
