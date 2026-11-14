# oh-my-grok against OmO and Hermes

Read-only architecture review. No product code was changed. `spec/IMPLEMENTATION.md` was not opened for edit and its hash is untouched.

The owner’s worry is right about several files and wrong about the bus. oh-my-grok copied OmO’s role names, the `.omo/boulder.json` shape, and a skill named git-master. Those are the reinventions. The SQLite outbox, the revision-bound gate, the evidence levels, and the sandbox are not in either upstream, and they are the part that belongs on Grok Bot. Porting OmO’s plugin or Hermes’s agent loop would throw away the only reason this repo exists.

## Pins

| Tree | Commit | Date read | What it is |
|---|---|---|---|
| oh-my-grok (this repo) | `2f6b3041903788eeb8307185b466029c4aff5cf8` on `main` | 2026-11-14 | SQLite workflow bus that wakes Grok Bots over HTTPS |
| [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) `dev` | `1e3359770ea9a00fe9ec431a95a0c9a8dfeba132` | 2026-11-14 | OpenCode / Senpi / Codex harness. npm name remains `oh-my-opencode` |
| [hermes-agent](https://github.com/NousResearch/hermes-agent) `main` | `cd582ed706cec677572d4da6f1c0dbdc5ca8c410` | 2026-11-14 | One Python process: agent loop, messaging gateway, cron |

OmO is three harnesses on shared libraries, plus a separate desktop/computer stack. The harness that matters here is `packages/omo-opencode` (OpenCode plugin), with Senpi (`packages/omo-senpi`, `packages/senpi-task`) and Codex (`packages/omo-codex`) repeating the same ideas. `packages/omo-computer-*` and `packages/senpi-desktop-*` are a computer-use product. Do not treat them as the orchestrator.

Hermes is one `AIAgent`. `run_agent.py` is a facade. The loop is `agent/conversation_loop.py` plus `agent/turn_*.py`. The gateway (`gateway/run.py`) is a long-running daemon. Cron runs inside that daemon on a 60-second tick (`cron/scheduler.py`, `cron/jobs.py` `TICKER_INTERVAL_SECONDS = 60`).

oh-my-grok is neither. `oh_my_grok/engine.py` is the only transition authority. `oh_my_grok/mcp_server.py` and `oh_my_grok/cli.py` are two fronts on that engine. Wake-up is an outbox POST of ids (`docs/ARCHITECTURE.md`).

## Top 10, by value per unit of work

Ranked so the first items are decisions, not projects. Later items cost real engineering.

1. **Freeze the boulder projection.** Stop adding upstream fields. `.omo/boulder.json` in OmO is read back to continue work. Ours is a write-only mirror under `jobs/<job_id>/.omo/`, which is not the worktree, so an upstream tool would not see it even if one were running. Continuation is already `omg.status`, the reconcile routine, and the watchdog. See [Boulder](#boulder-and-todo-continuation).

2. **Do not build a healer bot.** `spec/HEALING.md` slices 2–4 add a role, a diagnosis, and an enqueued job. Slice 1 already records the corpus and, in every mode including `enforce`, enqueues nothing. A ninth wake does not heal. The next Hephaestus turn can read `omg heal show`. See [Healing](#healing-and-error-recovery).

3. **Make the reconcile routine the thing that survives a restart.** Platform schedules come back after the computer is replaced. The watchdog does not. The routine text in `routines/omg-reconcile.md` should call `omg watchdog ensure` and then `omg.reconcile`. Fifteen minutes is legal. Five minutes is the platform floor. See [Scheduling](#scheduling-cron-and-the-watchdog).

4. **Keep the bus.** Do not replace it with OmO’s `task` tool, Hermes `delegate_task`, or a Grok subagent. Those three share a process or a parent session. This system’s job is to wake other bots and not trust their prose. See [Bus](#bus-and-orchestration-loop).

5. **Keep the gates, the evidence levels, and the sandboxed language-server run.** Do not port `packages/lsp-daemon`, hashline edit hooks, or the comment checker. Those run inside a session the model already trusts. Ours exist because a bot on this account can write `PASS` and can edit the database. See [Gates](#gates-verifier-lsp-evidence).

6. **Keep the roster floor. Do not add Sisyphus, Atlas, Prometheus, or Oracle as bots, and do not import OmO categories.** Categories exist to pick a model per lane (`packages/senpi-task/src/category/builtins.ts`). Grok Bot picks the model. Every extra role is a webhook and a rate-limit slot. See [Roles](#roles-and-roster).

7. **Stop presenting the TTY receipt as the user-facing approval.** Approval cards are the platform’s control, including on a phone. `omg approve` requires a TTY on the shared computer, and a bot on that computer can fake one (`docs/SECURITY.md`). Ask-first on that command is the substitute. Do not implement the on-box Ed25519 protocol. See [CLI and approvals](#cli-and-approvals).

8. **Keep our git-master text.** Upstream `packages/shared-skills/skills/git-master/SKILL.md` exists at this pin (the adaptive spec’s “not found” is stale) and it tells the model to rebase and squash. `git_discipline` forbids that. The engine’s history checks are the real control. See [Skills](#skills).

9. **Point implementation at a Cloud Agent branch, and keep the local worktree as the verifier’s scratch copy of that SHA.** The frozen spec and `routines/executor.md` still commit on a worktree on the shared computer. Grok Bot’s documented coding path is a Cloud Agent on another computer. Do not spend effort making the local editor more like hashline. See [Where code changes](#where-code-changes).

10. **Do not build a second memory, a skill loader, or a cron daemon.** Memory is per bot. Skills are a platform library. Routines are the scheduler. Hermes `~/.hermes/cron/jobs.json` and OmO `packages/memory-core` both assume a home directory and a process you supervise. See [Memory](#memory) and [Scheduling](#scheduling-cron-and-the-watchdog).

## What each product is for

OmO keeps a multi-agent loop inside one coding session. A plugin hooks the host’s events (`session.idle`, `tool.execute.before`, `tool.execute.after`, message transforms). It injects text, rewrites tool calls, spawns child sessions, and reads `.omo/boulder.json` from the working directory when a run settles. Config is `omo.json` / `omo.jsonc` (`packages/omo-config-core`). The old filenames `oh-my-opencode.json` and `oh-my-openagent.json` are migrations, not the live path.

Hermes keeps a conversation loop in one process and a gateway that turns Telegram, Discord, Slack, and the rest into that loop. Tools self-register. Subagents are more `AIAgent` objects in the same process (`tools/delegate_tool.py`, `delegate_task`). Sessions live in `~/.hermes/state.db`. Killing the process drops the loop, the cron thread, the MCP children, and the in-memory approval wait. The database and `cron/jobs.json` survive.

oh-my-grok keeps jobs in SQLite and pings bots. Eight logical roles, six bot ids in `config/bots.json`, with code-reviewer on Momus and qa-executor on Metis (`config/roles.example.json`). Hephaestus talks to the user and calls MCP. It does not webhook itself for intake. Feature flags default to `off` and freeze into the job at submit (`oh_my_grok/features.py`). The install is not operational until a live cross-bot run is recorded (`spec/ADAPTIVE.md` section 12). That is unchanged by this review.

## Subsystem verdicts

| Subsystem | Reinvented? | Verdict | Why |
|---|---|---|---|
| Bus / outbox | No | Keep | Neither upstream wakes separate bots. Their loops are in-process |
| Roles | Names only | Keep the eight. Do not grow the cast | Model routing and harness modes do not exist here |
| Roster floor | No | Keep | It is a wake budget. OmO categories are a model budget |
| Boulder file | Yes | Freeze, then delete the compat layer | Shape-compatible with a reader we do not have and a path upstream would not look at |
| Plan checklist for humans | Adapted | Keep, as a render of the database | Useful. Not a continuation engine |
| Background work | Mechanism is ours | Keep outbox parallelism | Their background tasks need a live parent session |
| Model hooks | Name collision only | Do not port | `oh_my_grok/hooks.py` stores webhook secrets. It is not a hook system |
| Session recovery | No | Keep reconcile, leases, backup | Their recovery rewrites the model transcript |
| LSP daemon | No, and do not start | Keep per-run sandboxed servers | A daemon dies on computer reset and is the wrong trust model |
| Gates / verifier / evidence | No | Keep | This is the part they do not have, because they trust the session |
| Healing corpus | Partially | Keep slice 1. Do not build slices 2–4 | Their healing is in-loop. A healer bot is a new invention |
| Watchdog | Adapted | Keep, behind the routine | Same job as Hermes’s ticker, without a supervisor to respawn it |
| Memory | Not built | Do not build | Platform memory is per bot. A second store will drift |
| Skills loader | One packet field | Keep the packet and the git checks | Loaders need prompt injection |
| Cron / jobs.json | Not built | Do not build | Routines already schedule, with a 5-minute floor |
| Gateway | No | Do not build | Grok Bot is the gateway |
| Subagent spawn | No | Do not use subagents for gated roles | A subagent shares the parent session |
| Config schema | No | Keep `policy.json` small | `omo.json` is a harness config. Most keys would be dead |
| MCP control plane | No | Keep | The host spawns it. Hermes spawning its own does not fit |
| CLI | No | Keep as the operator tool | The user-facing approval should be a card |
| Local worktree as the editor | Yes, relative to the platform | Stop growing it | Cloud Agent is the documented coding path |
| Desktop / browser driver | Not built | Do not build | One computer-use task per bot screen is already the platform |

## Bus and orchestration loop

**oh-my-grok.** `submit` builds a DAG. Research tasks can run together. Plan, Momus, a human approval of the exact plan hash, an executor, a deterministic verifier, review and QA on that revision, integrate, and a final gate that is code (`docs/ARCHITECTURE.md`, `oh_my_grok/engine.py`). Dispatch writes an outbox row in the same transaction that makes a task ready, then POSTs `{schema_version, job_id, task_id, dispatch_id, attempt}` with a bearer key. HTTP 200 means the platform accepted a run. Completion is a lease-checked `claim` and `complete`. Hephaestus tasks with no verified resume routine sit in `awaiting_resume`. There is no self-webhook.

**OmO.** The user message hits the plugin (`packages/omo-opencode/src/plugin/`). Keyword detection injects an ultrawork or team prompt (`packages/omo-opencode/src/hooks/keyword-detector/`). Delegation is the `task` tool (`packages/omo-opencode/src/tools/delegate-task/`) and `call_omo_agent`. Senpi’s spine is `packages/senpi-task` (`task`, `task_send`, `task_cancel`, `task_output`) with in-process, RPC, and host-daemon runners. Team mode (`packages/omo-opencode/src/features/team-mode/`, `packages/team-core`) stores a mailbox on disk and polls it. None of this is a durable cross-process job with an untrusted peer.

**Hermes.** `AIAgent.run_conversation` loops until the model stops calling tools or the iteration budget runs out (`agent/conversation_loop.py`, `agent/iteration_budget.py`, default on the order of hundreds of iterations for a parent). Tool rounds can use a thread pool (`agent/tool_executor.py`). The gateway (`gateway/run.py`, `gateway/run_inbound.py`) authorizes a platform message, builds a session key (`gateway/session.py`), and runs that same loop. A busy session queues the next message. `/approve` is allowed through while the turn is blocked (`gateway/run_busy.py`).

**Verdict.** Keep the bus. It was not copied from either loop. Replacing it with `delegate_task` or `task` would require a long-lived parent, which this platform does not give the product. The README’s mention of `spawn_agent` is stale: current OmO delegates through `task` / `call_omo_agent` / Senpi `task`.

## Roles and roster

**oh-my-grok.** Eight roles in the frozen spec: hephaestus, explorer, librarian, metis, momus, executor, code-reviewer, qa-executor. Intent is `explanation` (no job), `investigation` (read-only, done from analysis), or `implementation`. For implementation, `oh_my_grok/roster.py` computes a floor from five attested fields: `heavy` if security-sensitive, a new dependency, or more than 15 files; `standard` if cross-cutting, an external API, or more than 3 files; otherwise `quick`. The caller may go up, never down. A diff that contradicts the attestation escalates. Post-approval work is the same for every class (`spec/ADAPTIVE.md` sections 4–5). Flags default off, so a job with no intent still gets the original full DAG.

**OmO.** Eleven built-in agents under `packages/omo-opencode/src/agents/` (Sisyphus, Hephaestus, Prometheus, Atlas, Metis, Momus, Oracle, Librarian, Explore, Multimodal-Looker, Sisyphus-Junior), with prompts in `packages/prompts-core/prompts/`. Prometheus plans. Atlas orchestrates execution. Sisyphus is the ultraworker. Categories in `packages/senpi-task/src/category/builtins.ts` bind a lane to a model chain. Per-agent tool denylists live beside the agents. Hephaestus in OmO is a model-specific primary agent, not “the bot the user talks to.”

**Hermes.** No fixed cast. `delegate_task` takes an optional `role` and a toolset. Children drop `DELEGATE_BLOCKED_TOOLS` (`tools/delegate_tool.py`). The model decides when to delegate. There is no Momus-shaped plan-hash check in the engine.

**Verdict.** The names were borrowed. The roster math was not, and it should stay, because a wake costs a routine run. Importing Atlas, Prometheus, and Sisyphus would add bots that exist upstream only because one session can switch model and prompt. Grok Bot will not route models per role (`spec/IMPLEMENTATION.md` section 3, still true). Quick/standard/heavy is a cost control. It is not a smaller OmO.

## Boulder and todo continuation

**oh-my-grok.** With `features.boulder=on`, `oh_my_grok/projection.py` writes `jobs/<job_id>/.omo/boulder.json` and a plan markdown file. The writer takes an exclusive lock, renders from a read snapshot, refuses to regress the generation, replaces atomically, and overwrites tampering. The engine never reads the file back (`spec/ADAPTIVE.md` section 6). `omg.status` returns `active_work`. `agents/hephaestus-omg.md` tells Hephaestus to call status first and not to resume by editing the boulder. Session ids are `oh-my-grok:<bot_id|role>:<attempt_id>` because no platform session id is visible.

**OmO.** The schema is `packages/boulder-state/src/types.ts`: `schema_version?: 2`, `works`, mirrored `active_plan`, `session_ids`, `task_sessions`, statuses `active|completed|paused|abandoned`. The file is `<project>/.omo/boulder.json` (`packages/boulder-state/src/constants.ts`). `writeBoulderState` is `mkdir` plus `writeFileSync` (`packages/boulder-state/src/storage/write-state.ts`). It is not locked and not atomic. That was true at the pin cited in `spec/ADAPTIVE.md` and it is still true at `1e33597`.

Two different continuations sit on top:

- OpenCode `todoContinuationEnforcer` (`packages/omo-opencode/src/hooks/todo-continuation-enforcer/`) listens for `session.idle`. Incomplete session todos get a countdown and an injected continuation prompt. State is in memory. This path does not read `boulder.json`.
- Senpi `ulw-execute-continuation` (`packages/omo-senpi/src/components/ulw-execute-continuation/index.ts`) runs when the agent settles, reads `.omo/boulder.json` in the working directory, and injects a reminder whose text says to read that file and finish every top-level checkbox. Cap is 8 consecutive continuations. Codex has the same component under `packages/omo-codex/plugin/components/ulw-execute-continuation/`.

Checklist grammar (`packages/boulder-state/src/plan-checklist.ts`): `## TODOs`, `- [ ] 1. ...`, `## Final Verification Wave`, `F1`, `[~]` counts as not done for continuation.

**Hermes.** `tools/todo_tool.py` keeps a `TodoStore` on the agent object. After compression the store is re-injected. `hermes_state_todo.py` can read the last todo tool result out of SQLite for display. Nothing scans a plan file on an idle event. Continuation across process death is “the session row is still there, start another turn.”

**Verdict.** The atomic writer is better engineering than `writeFileSync`, applied to a file with no reader. Upstream reads a boulder in the project working directory. Ours is stored beside the job, outside the worktree, specifically so the executor cannot commit it (`spec/ADAPTIVE.md` section 6.1). Shape compatibility is unused on both sides. Freeze it. A later change can delete the compat fields (`x_omg`, fake session ids, `stale` reconciliation against transcripts we do not have) and keep the plan markdown as a human rendering of the database. Do not port the idle enforcer. There is no `session.idle` to hook. The substitute that already exists is status-first, `awaiting_resume`, and reconcile.

## Background tasks

**oh-my-grok.** Explore and research run in parallel when they have no dependency and their bots have free slots. `max_concurrent` is counted across roles that share a bot, because one bot cannot take two computer-use turns. An hourly cap defaults to 20 dispatches per bot (`docs/ARCHITECTURE.md`). Re-wakes of an accepted-but-unclaimed task are capped (`max_rewakes`, default 3).

**OmO.** `BackgroundManager` (`packages/omo-opencode/src/features/background-agent/manager.ts`) queues children per provider/model, polls about every 3 seconds, and notifies the parent on idle. A loop detector breaks repeated identical tool calls. Senpi can park completions during compaction and can run children through a host daemon (`packages/senpi-task`, described in `docs/reference/omo-daemon.md`). Spawn depth is a config (`task.max_depth`, schema default 1 in `packages/omo-config-core`).

**Hermes.** `delegate_task` runs the child on a thread in the parent process. Background delegation writes `async_delegations` / `resumable_subagents` in the same SQLite file and delivers the result as a later parent turn (`tools/async_delegation.py`). The child shares the process and the MCP loop. It does not share the parent’s in-memory agent.

**Verdict.** Keep the outbox. Their background tasks are a live parent waiting on children. A Grok routine returns. The parent is gone. Bounded re-wake is the substitute, and it is already more honest than a 3-second poll that cannot exist here.

## Hooks

**oh-my-grok.** `oh_my_grok/hooks.py` stores webhook URL and bearer key in `state/secrets/hooks.json`, mode 0600. That name is a collision. There is no pre-tool or post-tool API. Routine instructions tell the model what not to do. The engine rejects illegal transitions after the fact.

**OmO.** The OpenCode plugin composes on the order of fifty hooks (`packages/omo-opencode/src/hooks/`, registry in the plugin config schema). The ones that carry the product: keyword and ultrawork injection, `todo-continuation-enforcer`, atlas boulder orchestration, `comment-checker` and `hashline-read-enhancer` on tool execute, rules injection, write guards, compaction capture, anthropic context-limit recovery, runtime model fallback, delegate-task retry. Senpi uses `session_start` and `agent_settled` instead of a hook named `SessionStart`. Disabling is `disabled_hooks` in `omo.json`.

**Hermes.** Two layers. `agent/shell_hooks.py` can block a tool (exit 2) by running an allowlisted script. `hermes_cli/lifecycle.py` has observer hooks (`pre_api_request`, `post_api_request`, `pre_tool_call`, `post_tool_call`, `transform_tool_result`). `hermes_cli/middleware.py` wraps tool execution. These are installed in the process that owns the loop.

**Verdict.** Do not port either hook system. Grok Bot does not let this product register a callback on the model loop. Enterprise inference hooks are a different thing: an organization security server that can allow or deny a model request, and with Validate Tool Calls can judge a tool call before it runs. They are beta, Enterprise-only, and they omit memory tools, the skill loader, system prompts, and hidden reasoning ([Grok Bot inference hooks](https://docs.x.ai/grok-bot/inference-hooks)). They are not a place to inject a continuation prompt. The substitute is the gate: if the artifact, the revision, or the receipt is missing, the job does not move.

## Session recovery

**oh-my-grok.** The database is the session. Leases expire. `reconcile` requeues, retries uncertain sends, re-runs a verifier that was killed mid-run, and flushes the outbox (`docs/RUNBOOK.md` section 8). `omg backup` / `omg restore` use the SQLite backup API and an integrity check. A restart of the MCP process loses nothing that was committed. A restart of the computer kills the watchdog and any in-flight routine; the rows remain.

**OmO.** Recovery is transcript surgery. Compaction hooks preserve todos and inject a continue (`packages/omo-opencode/src/plugin/` compaction path, `todo-continuation-enforcer/compaction-guard.ts`). `anthropic-context-window-limit-recovery` retries after a token-limit error. Thinking-block validators reuse signed blocks. Stale boulder works are demoted by looking at session transcripts on disk (`packages/boulder-state/src/storage/stale-work.ts`). All of that requires the host to tell the plugin that compaction happened.

**Hermes.** `hermes_state.py` (`SessionDB`) is WAL SQLite: messages, compression, rewind (`hermes_state_rewind.py`), FTS, repair (`hermes_state_repair.py`), a process-wide corrupt latch (`hermes_state_health.py`). `/undo` and `/retry` rewrite the session. Startup watchdog `hermes_startup_watchdog.py` calls `os._exit(75)` if initialization wedges, which assumes something outside will restart the process.

**Verdict.** Keep reconcile, leases, and backup. Do not port transcript rewrite or exit-75. The platform summarizes long conversations itself ([inference hooks](https://docs.x.ai/grok-bot/inference-hooks) covers “the summaries Grok Bot writes of long conversations”). There is no published knob for when that happens. Anything a later turn must know goes in SQLite and comes back through `omg.status`. Chat memory is the wrong place for a job id, and `agents/hephaestus-omg.md` already says that.

## Gates, verifier, LSP, evidence

**oh-my-grok.** After the executor reports a commit, the engine checks the commit is on the job branch and descends from the base, then runs a verifier profile (`oh_my_grok/verifier.py`) inside bubblewrap (`oh_my_grok/sandbox.py`): no network, private `/tmp`, read-only `/usr`, no view of state, code, or home. Fixture profiles use a trusted oracle (`oh_my_grok/oracles.py`) that the candidate never imports. A template marker or a missing profile fails closed. The final gate re-runs verification on the exact revision, requires reviews of that revision, checks artifact hashes, requires a clean worktree, and checks reviewer independence. With `lsp=enforce`, `oh_my_grok/lsp/` talks JSON-RPC to a real server (ruff pull diagnostics, basedpyright and typescript-language-server push diagnostics), runs a canary file first, and fails on a base-versus-candidate delta. Other code languages fail closed with `diagnostics_unavailable` (`spec/ADAPTIVE.md` section 7). A gate accepts `engine_executed` or `engine_reverified` only. `engine_delivered` and `agent_claimed` never pass a gate. `repo_lease` (default off) is one editing job per repository path (`oh_my_grok/engine.py`).

**OmO.** Diagnostics are a tool the model calls. `packages/lsp-daemon` is a detached per-user daemon (unix socket or Windows pipe, idle exit around 30 minutes, state under `~/.omo/lsp-daemon/`). The OpenCode plugin registers it as a built-in MCP server (`packages/omo-opencode/src/mcp/`). `packages/lsp-tools-mcp` and `packages/lsp-core` sit behind that. Hashline (`packages/hashline-core`) and the comment checker (`packages/comment-checker-core`) are hooks on read and on tool-execute-after. There is no engine that re-runs a verifier on a commit the model cannot see and then refuses `done`.

**Hermes.** Optional `lsp_diagnostics` on a patch apply (`tools/patch_parser.py`). Terminal backends are local, Docker, SSH, Modal, Daytona, Vercel (`tools/terminal_tool_backends.py`). The model is the reviewer unless a human card or a shell hook stops a command. No revision-bound multi-agent gate.

**Verdict.** Keep every gate. This is the opposite of a reinvention: upstream does not have an untrusted second bot, so they do not need one. The language-server work is the expensive piece that is still worth it, because a CLI checker renamed “LSP” was specifically forbidden and because the canary catches a silent server. Do not adopt the daemon. Computer Update, Recover, and Reset drop installed packages and interrupt running work ([Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)). A daemon also holds diagnostics for a live editor session, which is not what a gate needs. A gate needs the bytes of one commit, in a sandbox, once.

`git_discipline` stays. It checks linear history, a trailer, a commit budget, and descent from earlier candidates. That is an engine fact. The skill text is not.

## Healing and error recovery

**oh-my-grok.** Slice 1 (`oh_my_grok/healing.py`, `spec/HEALING.md`) projects blocked jobs, repair-cycle events, rejected reviews, repeated failures, and stalls into `failure_snapshots` / `failure_items`. Collect does not change a job. `shadow` and `enforce` write the same rows. Slices 2–5 (healer role, proposal, enqueue a normal job, lessons, GitHub review ingestion) are not implemented. Submit-time `ValidationError`s never become rows. The watchdog, when it is `on`, is what wakes a stall. Healing does not.

**OmO.** Recovery is local and automatic: edit and JSON error recovery, delegate-task retry, runtime and proactive model fallback, the background-task circuit breaker, unstable-agent babysitter on idle, anthropic context recovery, boulder stale-work demotion, memory reflection parked after repeated failure. Each one is a hook with session-scoped state.

**Hermes.** `agent/turn_recovery.py` and `agent/turn_api_error.py` retry, fall back to another model, and refresh credentials inside the loop. `hermes_state_repair.py` rebuilds a damaged database. Cron reaps a stale running claim on the next tick (`cron/`). The startup watchdog exits 75 so an external supervisor restarts the process.

**Verdict.** The corpus is a reasonable substitute for hooks you cannot register: the failure is already in the database, so write it down in one place. Calling `enforce` healing is a lie until a later slice enqueues, and that slice should not be a new bot. Hephaestus is already the integrator. It can read the latest snapshot on its next turn. A fix, if a human wants one, is an ordinary job with a plan hash and a review. Do not port model-fallback hooks. This product does not choose the model. Do not port exit-75. Nothing we install is allowed to restart us.

## Scheduling, cron, and the watchdog

**oh-my-grok.** `routines/omg-reconcile.md` is an optional Hephaestus routine, suggested every 15 minutes, not created by the install. It calls `omg.reconcile` once. It is not a ninth role. `oh_my_grok/watchdog.py` is a self-supervised loop because this host has no systemd and no crontab (PID 1 is tini, `docs/RUNBOOK.md` section 13). `off` writes a heartbeat. `shadow` reconciles, collects, and records stalls. `on` also POSTs a wake, capped, with backoff, and only when the role has a hook. `ensure` starts the loop if the lock is free. Nothing respawns it after reboot. The runbook says to point some external backstop at `ensure`. The platform backstop is a routine.

**OmO.** No user cron. Polls: background tasks ~3s, team mailbox ~3s, an OpenClaw reply listener ~3s. Reflection “scheduler” is a file lock triggered by events, not a clock. LSP daemon idle timer is 30 minutes.

**Hermes.** `cron/jobs.py` stores jobs in `~/.hermes/cron/jobs.json` per profile. The gateway’s `SupervisedTickerThread` calls `tick` every 60 seconds. A job builds a fresh `AIAgent` with `skip_memory` by default, runs the prompt, and delivers through the gateway. Timezone is per job (`cron/jobs_time.py`). A standalone `hermes cron tick` exists for an external kicker. In-flight state dies with the process; the JSON file survives. The design assumes the ticker is supervised.

**Verdict.** Do not port `jobs.json` or a 60-second ticker. Grok routines are the scheduler: user timezone, plain-language schedule, minimum five minutes apart, at most 50 routines per bot, 20 stored run records, 200 runs per hour per routine, 300 per hour across the account, and a shared platform limit that is not retried automatically ([Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)). A 3-second poll is not a degraded version of this. It is impossible.

Keep the watchdog as an accelerator while the computer is up. The durable schedule is the routine. The routine should call `omg watchdog ensure` before `omg.reconcile`, so a computer that came back gets a supervisor without a human. Fifteen minutes matches the existing suggestion and sits above the five-minute floor. Do not tighten it toward Hermes’s minute. Each run spends usage, and a rate-limit failure stays failed until something sends it again.

## Gateway

**oh-my-grok.** There is no gateway. Grok Bot receives the webhook, starts a routine, and the bot calls MCP. `oh_my_grok/transport.py` is a bounded HTTPS POST.

**OmO.** Not a chat gateway. Adjacent pieces: an OpenGateway provider that injects models, OpenClaw outbound webhooks plus an inbound reply daemon (`packages/openclaw-core`), a Senpi thread gateway for cross-session tools, and the LSP daemon as a multi-session fan-in. All of them assume a local harness process.

**Hermes.** `gateway/run.py` is the product’s front door. Adapters normalize platform events. Session keys, allowlists, pairing, a queue while a session is busy, slash commands, and a turn lease in `session_turn_leases`. One process can multiplex profiles. Cron lives here.

**Verdict.** Do not build one. Grok Bot already routes chat, routines, and webhooks. A second gateway would be a daemon on a box that gets replaced, duplicating a service we do not control. The outbox is the whole integration surface, and it should stay ids-only.

## Memory

**oh-my-grok.** No memory module. Job state is SQLite. Bot memory is the platform’s. `docs/MIGRATION.md` says a filesystem backup does not restore conversations or agent memory. Same-account bots can read the SQLite file; that does not make it a shared mind. The threat model says so (`docs/SECURITY.md`).

**OmO.** `packages/memory-core` is a git-backed markdown store with `memory` and `memory_apply_patch` tools, a reflection worker, a facts queue, and an agreements ledger. Senpi wires a reflection child and a recall wake. Locks live under an identity directory. Compaction can trigger reflection. This is a second agent with a worktree.

**Hermes.** `agent/memory_manager.py` loads `MemoryProvider` plugins. The default writes MEMORY.md / USER.md under the profile home. `prefetch_all` builds a block that `agent/turn_context.py` injects. `on_pre_compress` flushes memory before context is dropped, and that flush is fail-closed. Cron jobs skip memory on purpose. Providers are files plus session metadata, not a service.

**Verdict.** Do not port either. Each Grok Bot has its own memory: preferences, rules, a dated work log. A few facts (name, time zone) are shared. The bot decides what to store. There is no memory screen for a personal bot. Resetting a chat keeps memory. Deleting a bot deletes it. Memory writes are a side effect Auto Review does not cover ([How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works), [Create and manage Bots](https://docs.x.ai/grok-bot/bots), [Grok Bot security](https://docs.x.ai/grok-bot/security)). A git memory repo on the shared computer would be readable by every bot and would drift from the per-bot store. Workflow facts stay in SQLite. Lasting user preferences stay in that bot’s memory, said in chat by the user.

## Skills

**oh-my-grok.** `oh_my_grok/skills.py` loads `skills/git-master/SKILL.md` into the executor packet as `skill_pack` and records an `engine_delivered` receipt. Delivery is not obedience. With `git_discipline` not off, the engine checks the history shape itself (`spec/ADAPTIVE.md` section 9). The text says it was written for this repo.

**OmO.** The catalog is `packages/shared-skills/skills/` (`ulw-execute`, `ulw-plan`, `git-master`, `ultrawork`, `review-work`, and others). `packages/skills-loader-core` discovers them. They enter context through the Read tool, a slash command, a hidden pointer, or `load_skills` on a task. `git-master` at this pin is a general git skill: COMMIT, REBASE, HISTORY, STATUS. It tells the model to rebase, squash, and fixup when that is the ask, and it treats an ulw workflow as the ask. That is the opposite of our trailer-and-no-rewrite rule.

**Hermes.** Bundled skills under `skills/` and `optional-skills/` are selected into the system prompt (`agent/prompt_builder.py`, `agent/skill_commands.py`). A slash skill arrives as a user message so the system prompt stays stable. `tools/skill_manager_tool.py` lets the model create a skill, behind a write approval, staged under `~/.hermes/pending/skills/`.

**Verdict.** Keep our text. Adopting upstream git-master would teach the executor to rewrite history the gate then rejects. The adaptive spec’s note that the upstream file was missing is stale at `1e33597`; the decision does not change, because the file that exists is the wrong policy. Do not build a loader or a `skill_manage`. Grok skills are a library the bot may or may not open ([Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)). The packet is how we know which bytes were offered. The git checks are how we know what happened. Saving the same text as a platform skill on the executor bot is optional and does not replace the receipt.

## Subagent delegation

**oh-my-grok.** Delegation is a webhook to another bot, or a local `awaiting_resume` row for Hephaestus. Depth is the DAG, not a spawn counter. Worker routines forbid messaging other bots to bypass the bus, creating routines, and editing the database (`routines/executor.md` and the siblings).

**OmO.** `task` and `call_omo_agent` spawn a child session with a tool denylist. Oracle, Librarian, and Explore cannot themselves delegate. Unstable agents are forced into the background. Depth is `background_task.maxDepth` / `task.max_depth`. Children have their own session transcripts.

**Hermes.** `delegate_task` (`tools/delegate_tool.py`) builds a child `AIAgent` in-process. The parent sees a summary. Background results re-enter on a completion queue. Blocked tools and a lower iteration budget are the containment.

**Grok Bot.** Subagents exist. Auto Review treats “Cloud Agent and subagent launches” as delegation it can allow, ask, or deny ([Grok Bot security](https://docs.x.ai/grok-bot/security)). Inference-hook framing says subagents use the parent’s session and are reported under that session id ([Grok Bot inference hooks](https://docs.x.ai/grok-bot/inference-hooks)). I did not find a public sentence that says a subagent is incapable of writing memory. Auto Review explicitly does not review memory writes. The owner’s “subagents cannot edit routines or memory” is therefore only half confirmed: routine edits are an automation write that Auto Review does cover, and our routine text forbids them; memory writes are unreviewed and per bot, not a proven hard denial.

**Verdict.** Gated roles stay on the bus, as separate bots. A subagent of Hephaestus is still Hephaestus for assurance purposes. Using one as Momus would make `independent_code_review_and_qa` a lie. A subagent may do ungated reading inside a single turn. That result is `agent_claimed`. It cannot satisfy a gate.

## Config

**oh-my-grok.** Optional `config/policy.json`, unknown keys rejected. Repair caps, lease, rewake, backoff, hourly cap, repo allowlist, and the feature map. Features freeze into `jobs.policies_json` at submit. `config/verifiers.json`, `config/lsp.json`, `config/bots.json`, `config/roles.example.json`. Secrets are not in those files.

**OmO.** `omo.json` / `omo.jsonc`, user file plus project files, schema `packages/omo-config-core/src/schema/config.ts`. Agents, categories, permissions, hooks, background-task limits, models, harness-specific blocks for OpenCode, Senpi, and Codex. Hot reload exists on Senpi.

**Hermes.** `~/.hermes/config.yaml` per profile, secrets in `.env`, `HERMES_HOME` overridable (`hermes_constants.py`). One gateway process can host many profiles with a scope binding.

**Verdict.** Keep the policy file small. An `omo.json` port would be a map of features this process cannot perform (model chains, hook names, tmux, categories). Frozen-at-submit is the right idea for a job that outlives a config edit, and Hermes does not need it in the same way because a Hermes turn reads config as it goes.

## MCP server

**oh-my-grok.** `oh_my_grok/mcp_server.py` is a stdio server the host launches. Tools: `submit`, `claim`, `context`, `checkpoint`, `complete`, `status`, `reconcile`, `cancel`, `escalate`, `docs`, `heal`. No approve, ship, shell, or secret tool. A fingerprint of code and config refuses to serve if the process is stale. Logs go to stderr and are redacted. `context7.py` is the other direction: the engine calls Context7 and stores a receipt. A bot’s citation counts only when the quote is in that stored response.

**OmO.** The plugin *is* the host integration. MCP is how it exposes LSP, ast-grep, and git-bash to the session (`packages/lsp-tools-mcp`, `packages/ast-grep-mcp`, `packages/git-bash-mcp`, `packages/mcp-client-core`). The plugin registers those servers. It is not a control plane the host discovers.

**Hermes.** `tools/mcp_tool.py` spawns and supervises stdio and HTTP servers on a daemon thread, reconnects with backoff (`tools/mcp_tool_health.py`), and registers tools when discovery succeeds. The agent owns the child process.

**Verdict.** Keep the server as a host-spawned command connector. Docs: a command MCP runs on the computer and is then available to the other bots; a private network or a private CA asks for approval first ([Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)). Discovery can fail while the Python file is fine. The owner’s “server exited” is that class of failure. The substitute is operational: `omg doctor`, `omg mcp-command`, restart the connector, and the stale-fingerprint refusal so a half-reloaded process does not keep serving old code. Do not take over process supervision the way Hermes does. The host owns this process. The Context7 client is fine because the engine, not the bot, makes the call and stores the bytes.

## CLI and approvals

**oh-my-grok.** `omg` (and the `omoctl` shim) is the operator CLI: doctor, migrate, status, submit, plan, approve, deny, cancel, resume, reconcile, export, backup, restore, hooks, roles, heal, lsp, boulder, features, attest, watchdog, tests (`oh_my_grok/cli.py`). `approve`, `deny`, `cancel`, `restore`, and `attest` require a real TTY and refuse a pipe. Approve checks the job, the plan hash, Momus’s acceptance, the requirements hash, and the scope, then asks the human to type 12 hex characters. The receipt is mode `soft`. Strict external signatures are a protocol in `docs/SECURITY.md` and are not implemented. The engine refuses production merge and deploy. `cancel` on MCP is not authorization; the CLI is.

**OmO.** Permissions are host dialogs and `tool.execute.before` denials. There is no plan-hash receipt stored outside the session. The user is already in the coding session.

**Hermes.** `tools/approval.py` detects dangerous commands, can call a guardian model, and waits. On the gateway the wait is unblocked by `/approve` or `/deny` in chat (`gateway/run_busy.py`). Pending state is in memory. A permanent allowlist is config. Killing the process drops the wait.

**Verdict.** Keep the CLI. Change what we tell the user to do. Grok shows an approval card in the conversation, on desktop and on iPhone, with Allow once, Always allow, and Deny ([How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works)). Auto Review covers shell, app calls, computer use, automation writes, and cloud-agent or subagent launches. It does not cover memory writes or most settings ([Grok Bot security](https://docs.x.ai/grok-bot/security)). A TTY prompt on the shared computer is a poor match: the human may be on a phone, the agent has no TTY at attestation time, and a bot that can run `omg` can fake `isatty` (the security doc already says this). The substitute is an Ask-first rule on `omg approve`, `omg hook set`, `omg restore`, `omg roles bind`, routine edits, and `git push`, which `docs/SECURITY.md` already recommends and which this install has not verified. The card still has to be bound to the plan hash. A card that says “run this command” without the hash is not an approval. Do not build the Ed25519 flow until the private key lives off this computer. A key generated here is not an external authority.

## Where code changes

**oh-my-grok.** `oh_my_grok/gitops.py` creates a per-job worktree and branch `omg/<job-id-prefix>`. Repo hooks are disabled. The engine never resets, cleans, force-pushes, or deletes user branches. The executor routine tells the bot to commit in `packet.worktree_root` and report a 40-character SHA. The verifier re-checks that SHA.

**OmO.** Edits happen in the session’s working tree, with hashline edits so a stale read cannot patch the wrong line, and with hooks that reject writes outside the rules. ulw-execute lands branches as part of the workflow. The boulder’s `worktree_path` is that tree.

**Hermes.** The terminal tool runs locally or in Docker, SSH, Modal, Daytona, or Vercel. Subagents can get a worktree. Patches can ask for LSP diagnostics. The agent is sitting on the tree it edits.

**Platform.** “Bots can also hand coding work to Cursor Cloud Agents and attach the results to their reply” ([How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works)). Team admins can disable that delegation. The work runs on separate computers under existing Cloud Agent controls ([Grok Bot for teams and enterprises](https://docs.x.ai/grok-bot/teams-and-enterprises)). Auto Review sees the launch.

**Verdict.** The local worktree is what the frozen spec required, and it is the wrong long-term editor. It reinvents OmO’s worktree on a computer every bot shares, with a venv that a computer update will throw away. The bus should record a Cloud Agent branch and the SHA it produced, and the candidate revision should be that SHA. Keep `gitops` and bubblewrap as the place a fetched tree is verified, not as the place the product grows an editor. Do not port hashline. Hashline pays off when the same session reads and writes between tool calls. A cloud agent has its own editor. Merge stays outside this box, which the engine already enforces.

## Desktop and browser

Not a subsystem we have, and not one to add. OmO’s computer crates and Hermes’s browser supervisor (`tools/browser_use_cli.py`, a CDP supervisor per task) assume the agent process can drive a display.

Grok Bot’s rule is narrower than “only one desktop driver.” Every bot on the account shares one computer, one browser profile, and one set of command-line credentials. Each bot has its own screen, so several bots can use computer tools at the same time. One bot runs only one computer-use task on its screen at a time ([Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps), [How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works)). Signing in for one bot signs in the others.

**Verdict.** Do not build a driver. The bus already serializes a bot through `max_concurrent`. Two roles that both need the browser must be the same bot or they will fight over one login. Prefer a connected app over the browser when an app exists. The hook-setup form in `oh_my_grok/hook_form.py` is a loopback page for pasting webhook secrets. It is not a browser automation stack, and `docs/SECURITY.md` already says a same-account process can read it.

## Platform constraints

Each row is a place Grok Bot cannot behave like OmO or Hermes, the feature that fact blocks, and the substitute that is actually available. Sources are this repo and the public Grok Bot docs fetched on 2026-11-14. Where the owner’s starting point was sharper or softer than the docs, the row says so.

1. **You do not own a process that lives across turns.** A bot wakes from chat, from a routine (schedule, webhook, Slack, email, GitHub, Linear, Sentry, PagerDuty), or continues inside a turn the platform owns. This host’s PID 1 is tini and there is no crontab (`docs/RUNBOOK.md` section 13, `oh_my_grok/watchdog.py`). Update, Recover, and Reset interrupt running work and drop installed packages and virtualenvs ([Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)). The starting point “agents are not long-lived” is slightly strong: a turn can keep going with the laptop closed, and background commands and cloud agents can outlive a conversation reset ([inference hooks](https://docs.x.ai/grok-bot/inference-hooks)). What you cannot do is install a supervisor.
   - **Blocks:** Hermes gateway and in-process cron ticker; OmO `session.idle` continuation; `lsp-daemon`; the Senpi task daemon; Hermes exit 75.
   - **Substitute:** SQLite as the only authority. A Hephaestus routine every 15 minutes runs `omg watchdog ensure` and then `omg.reconcile`. The watchdog is an accelerator between those runs.

2. **Routine schedules have a five-minute floor, in the user timezone.** Also 50 routines per bot, 20 retained run records, 200 runs per hour per routine, 300 per hour for the account, and a shared platform limit that fails the run and does not retry it ([Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)). A new schedule waits for the next time. HTTP 200 means a run started, not that it finished. The same 200-is-not-done rule is in `docs/ARCHITECTURE.md`.
   - **Blocks:** OmO’s ~3 second polls; Hermes’s 60 second tick; any design that continues “as soon as the model goes idle.”
   - **Substitute:** The existing per-bot hourly cap (default 20), capped re-wakes, and a blocked row a human resumes. Do not poll.

3. **One computer, separate chats, separate memories.** All bots share the machine, the browser logins, and the shell. Each bot has its own memory, plus a few shared facts such as name and time zone ([How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works)). They do not share an object graph. `docs/SECURITY.md` is the local statement of the same fact, including that a bot can read `state/omg.sqlite3` and `hooks.json`.
   - **Blocks:** OmO `BackgroundManager` and in-process team state; Hermes’s agent cache and in-memory `TodoStore`; treating two roles on one bot as independent reviewers.
   - **Substitute:** The outbox, content-addressed artifacts, and the independence label. `config/roles.example.json` already puts code-reviewer on Momus and qa-executor on Metis, and the gate still calls that `separate_bots_same_account` because the account is shared.

4. **No product hooks on the model loop.** OmO’s product is those hooks. Hermes’s shell hooks and observer hooks run in-process. Enterprise inference hooks can deny a request or, with Validate Tool Calls, a tool call. They do not inject text, do not see system prompts or the skill loader, and a personal account does not have them ([Grok Bot inference hooks](https://docs.x.ai/grok-bot/inference-hooks)).
   - **Blocks:** Ultrawork keyword injection, todo continuation, comment-checker, hashline enhancer, rules injector, compaction todo preserver, Hermes `pre_tool_call` blocking for product policy.
   - **Substitute:** Routine text for what the model should do. Engine transitions for what is allowed to count. A missing receipt fails the gate.

5. **Compaction and summarization are platform-owned.** The inference-hook docs mention summaries of long conversations and include them in covered traffic. There is no API in this repo or in those docs to set the token threshold or to re-inject a checklist after a summary.
   - **Blocks:** OmO compaction-guard and autocontinue; Hermes `ContextCompressor` and flush-before-compress.
   - **Substitute:** `omg.status` at the start of every Hephaestus turn, which `agents/hephaestus-omg.md` already requires. The job id and the next action live in the database.

6. **Command MCP servers are started by the host, then shared with every bot.** A discovery failure (“server exited” and its relatives) is independent of whether `oh_my_grok/mcp_server.py` would have served. The server also refuses to serve after its own files change until the connector restarts (startup fingerprint in `mcp_server.py`).
   - **Blocks:** Hermes’s in-agent MCP supervisor; OmO registering the LSP MCP from inside the plugin as the way the bus comes up.
   - **Substitute:** `omg doctor` and `omg mcp-command`, then reload the connector. Engine-owned clients such as Context7 stay, with a stored receipt, because the bot’s claim that it called a tool is not evidence.

7. **The human approves with a card, not a TTY.** Cards work in the chat, including on iPhone. `omg approve` refuses anything that is not a TTY (`oh_my_grok/cli.py`). The agent does not have that TTY at attestation time. A same-account bot can fake one.
   - **Blocks:** Hermes `/approve` inside a live turn; OmO’s permission dialog; this repo’s soft receipt as a security boundary.
   - **Substitute:** Ask-first Auto Review on the operator commands, with the plan hash in the thing the human approves. Strict signatures stay unimplemented until the key is off-box. The CLI remains the fallback for someone who is actually on the machine.

8. **Computer use is one task per bot screen, and the browser profile is shared.** The starting point “only one desktop/browser driver” overstates it. Several bots can run computer tools at once, on separate screens. One bot cannot run two computer-use tasks at once. Cookies are shared, so a login is account-wide ([Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)).
   - **Blocks:** OmO’s computer-use stack and Hermes’s per-task CDP supervisors; two bots automating the same logged-in site.
   - **Substitute:** Do not write a driver. Serialize a bot in the bus. Prefer a connected app. One role owns a browser workflow if a job truly needs one.

9. **Code edits are supposed to be a Cloud Agent on another computer, returning a branch and a PR.** Team policy can turn that delegation off. This repo still implements the frozen spec: the executor commits in a local worktree and does not push (`oh_my_grok/gitops.py`, `routines/executor.md`).
   - **Blocks:** OmO hashline and in-session landing; Hermes local/Docker/SSH as the editor; growing a local editor here.
   - **Substitute:** The bus stores the cloud-agent SHA as the candidate. The sandbox verifies a fetched copy of that SHA. The local worktree is scratch. Merge and deploy stay outside, which is already fail-closed.

10. **Subagents share the parent session.** They are not extra bots. Launches are reviewed. Routine edits are automation writes and are reviewed. Memory writes are not reviewed, and no public doc found for this review says a subagent is mechanically unable to touch memory.
    - **Blocks:** Using a subagent as an independent reviewer or QA; OmO’s per-agent tool denylist as a platform guarantee; Hermes’s child toolset as a security boundary.
    - **Substitute:** Reviewer and QA stay bound to other bots. Subagent output inside one turn is `agent_claimed` and cannot pass a gate. Worker routines keep the existing ban on creating routines and editing the database.

11. **Memory is per bot, opaque, and not restored from a disk backup.** Resetting a conversation keeps it. Deleting a bot deletes it. There is no personal-bot memory screen; you ask in chat ([Create and manage Bots](https://docs.x.ai/grok-bot/bots)). `docs/MIGRATION.md` matches this.
    - **Blocks:** OmO `memory-core` and its reflection children; Hermes memory injection and flush-before-compress; a shared lesson store that every bot writes.
    - **Substitute:** Job facts in SQLite. User preferences in that bot’s memory, entered by the user. Healing slice 5 (a lessons store) should not be built as a second memory.

12. **Skills load when the bot feels like it.** Private skills are one library, enabled per bot. There is no API to force a skill into the system prompt before a routine turn ([Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)).
    - **Blocks:** OmO slash rewrite and hidden skill pointers; Hermes auto-load into the system prompt; `skill_manage` from a child we do not trust.
    - **Substitute:** `skill_pack` in the task packet, hashed, plus an engine check of the consequences. Our git-master stays. Upstream git-master does not replace it.

13. **A card approves an action, not a plan hash, and it misses some side effects.** Allow once, Always allow, Deny. The bot does not retry a denial until the user sends another message. Memory writes and most settings changes are outside Auto Review.
    - **Blocks:** Treating a Hermes-style in-turn approval wait as the plan approval; treating a card on `git commit` as Momus plus the final gate.
    - **Substitute:** Cards for shell, push, routine edits, and delegation. The engine still requires the plan hash, the reviews, and the verifier. Those are different questions.

14. **Turn length and token budget are not ours, and the published pages do not give a number.** A bot cannot finish a turn while the computer is reconnecting ([Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)). Long conversations get summarized (constraint 5).
    - **Blocks:** Hermes’s large iteration budget and refund rules; OmO’s countdown that assumes the session is still the same context.
    - **Substitute:** Small packets, `checkpoint` to renew the lease (default 1800 seconds), and reconcile when the lease expires. Three repair cycles, then `blocked`.

15. **Same-account bots are one security principal.** Role checks are a cooperative state machine (`docs/SECURITY.md`). OmO `isolation-core` and Hermes profile directories assume the process next door cannot rewrite the database.
    - **Blocks:** Any claim that the MCP layer sandboxes a hostile bot. The bubblewrap sandbox is for candidate code, not for the bot.
    - **Substitute:** Say the assurance label out loud. Keep production credentials and merge rights off the box. The external CI note in `spec/ADAPTIVE.md` section 8 is the real boundary.

16. **A rate-limited routine stays failed.** The platform does not retry `Automation run rate limit exceeded` ([Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)). Our outbox does retry 429 and 5xx with capped backoff, and then blocks.
    - **Blocks:** Hermes and OmO in-loop retries that look like the product healing itself.
    - **Substitute:** Keep the backoff. When the budget is exhausted, stay `blocked` and require `omg resume`. Do not add a tighter loop to “be more reliable.”

17. **Hephaestus intake is a chat turn.** The spec forbids a self-webhook to start a job. Resume is optional and currently unhooked (`docs/MIGRATION.md`).
    - **Blocks:** OmO’s single session that plans, idles, and continues itself; Hermes’s gateway session that is still there when the next message arrives.
    - **Substitute:** `awaiting_resume` plus status on the next user message, and the reconcile routine for everyone else. Do not add a self-webhook to imitate idle continuation.

18. **Installed tools do not survive a computer update.** `scripts/bootstrap.sh` is how ruff, basedpyright, Node, and the TypeScript language server come back. `omg doctor` is how we notice they did not.
    - **Blocks:** A long-lived LSP daemon and a Hermes install that assumes yesterday’s binaries.
    - **Substitute:** Re-run bootstrap after Update, Recover, or Reset. Fail closed if a server is missing. That behavior already exists.

19. **This box does not merge or deploy.** Approval scope records `production_merge_or_deploy: false`. The engine never pushes.
    - **Blocks:** OmO ulw-execute landing branches; a Hermes terminal that pushes to main; any “the orchestrator finishes the PR” feature.
    - **Substitute:** A human, or a protected branch and CI that this computer’s credentials cannot satisfy.

20. **Group work is one bot per step.** The product’s own best practice is that a group chat assigns each step to one bot ([How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works)).
    - **Blocks:** OmO team mode’s shared mailbox and Hermes multiplexed profiles in one process, as a way to get parallelism inside one chat.
    - **Substitute:** The DAG. Parallelism only where the tasks do not share a bot and do not both need the same browser login.

## What not to port, as a list

- OmO hook catalog, keyword modes, ultrawork prompt injection, atlas hook, todo-continuation enforcer, ulw-execute continuation injector.
- OmO categories and model fallback chains. Sisyphus, Atlas, Prometheus, Oracle, Multimodal-Looker as further bots.
- `packages/lsp-daemon`, hashline, comment-checker, as integrations. The idea “run a real language server and fail on new diagnostics” is already implemented the right way.
- `packages/memory-core`, reflection children, Hermes `MemoryManager`, MEMORY.md injection.
- Hermes gateway, profile multiplexer, `jobs.json`, 60-second ticker, exit 75, in-process `delegate_task`.
- Upstream `skills/git-master/SKILL.md` as a drop-in. It authorizes rebase.
- A healer role, until a human has looked at real snapshots and still wants a job. Even then the job is a normal oh-my-grok job, which slice 4 already says, and Hephaestus can submit it.
- More boulder fields (`stale_since`, notepad directory, session transcript reconciliation). The writer is ahead of the reader. There is no reader.

## What is already the right shape

- SQLite as the only transition authority, with the boulder as a derived file when it is on.
- Ids-only webhook bodies, leases, idempotent claim, 200 is not done.
- Plan hash, Momus on that hash, human approval, invalidation when the hash changes.
- Verifier in a sandbox, oracle for fixtures, fail closed without bubblewrap.
- Evidence levels that refuse `agent_claimed`.
- Feature flags defaulting off and freezing into the job.
- Per-bot serialization and a low hourly cap.
- No approve, shell, or secret tool on the MCP server.
- Production merge refused.
- The watchdog’s honesty about tini and the missing crontab.

The README’s comparison table is still the right one-paragraph version. This note is the decision record for what to stop copying.

## Sources

Repository pins are in the table at the top. File paths are relative to those checkouts.

Grok Bot docs, fetched 2026-11-14:

- [How Grok Bot works](https://docs.x.ai/grok-bot/how-grok-bot-works)
- [Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)
- [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- [Grok Bot security](https://docs.x.ai/grok-bot/security)
- [Create and manage Bots](https://docs.x.ai/grok-bot/bots)
- [Grok Bot inference hooks](https://docs.x.ai/grok-bot/inference-hooks)
- [Grok Bot for teams and enterprises](https://docs.x.ai/grok-bot/teams-and-enterprises)

The Grok Build CLI subagent page (`docs.x.ai/build/features/subagents`) was not used. The frozen spec says that CLI is a different product, and its agent types are not Grok Bot’s.
