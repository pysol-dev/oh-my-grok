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

## Threat model: malicious candidate code (round 4)
A second, separate adversary is the **code under test**: the candidate revision a Bot commits may contain code
written to subvert verification. Momus round 4 showed that when the children ran as the box user without a
filesystem sandbox, candidate code could
P1 drop a `.pth` into the venv's site-packages (persisting into every later run),
P2 patch `omo_grok/verifier.py` (found via `/proc/self/cmdline`), and
S1 open `state/omo.sqlite3` via `../../..`, set `jobs.phase='done'` and read `state/secrets/`.

**What runs candidate code:** the unittest child, every oracle run, and every `exit_code_only` command. All of them
run inside the sandbox below; nothing else imports or executes candidate code (the verifier parent never does).

### Sandbox (`omo_grok/sandbox.py`)
- Mechanism: **bubblewrap** (`/usr/bin/bwrap`, 0.12.0 on this box, unprivileged user namespaces; kernel 6.12.94+).
  No root, sudo or extra OS users. `--unshare-all` (user, mount, pid, net, ipc, uts, cgroup namespaces),
  `--die-with-parent`, `--new-session`, `--cap-drop ALL`, `--clearenv`; rlimits AS 4 GiB, FSIZE 256 MiB, no core.
  The three oracle runs deliberately use different address-space limits (one random 256-512 MiB, one 4 GiB, one
  with no RLIMIT_AS at all; assigned at random to the run variants), see H1 (b).
- The sandboxed process sees only: a **read-only** bind of `/usr` (system Python and its stdlib; `/usr/local` is
  hidden behind an empty read-only tmpfs) with `/bin`, `/lib`, `/lib64`, `/sbin` symlinks; a **writable private
  copy** of the candidate checkout (no `.git`), fresh for every child run; a private `/tmp` tmpfs; its own `/proc`
  (own pid namespace) and a minimal `/dev`. Oracle runs additionally get a synthetic read-only `/etc`
  (passwd/group/hostname/hosts) and a private tmpfs home. Python is the system interpreter (`/usr/bin/python3.13`),
  not the venv.
- It has **no view** of `OMO_HOME/state` (DB, secrets, artifacts), `omo_grok/`, `config/`, the venv, `/home/box`,
  any other `/workspace` content, and **no network** (empty net namespace).
- **Fail closed:** if bwrap is missing or cannot create namespaces, verification returns
  `no sandbox available ...; refusing to run candidate code without a sandbox (fail closed)` and never runs the
  candidate. There is no configuration switch that disables the sandbox. `omoctl doctor` reports
  `candidate_sandbox`. The probe also requires `bwrap --version` to exit 0 and print exactly
  `bubblewrap <major>.<minor>[.<patch>]`; anything else fails closed. Every verification record stores the mechanism
  and version in `verifications.sandbox` (e.g. `bwrap 0.12.0`, or `none` when no candidate code ran).
- **Scratch cleanup** (`verifier._rmtree`) never follows symlinks and never chmods by path: entries are classified
  with lstat, directories are opened `O_NOFOLLOW|O_DIRECTORY` relative to their parent fd, and the only permission
  change is `fchmod` on such an fd after fstat confirms it is the directory inode that was listed.
- **Integrity tripwire:** sha256 of every file under `omo_grok/`, `config/` (and the active config dir), `omoctl`,
  `.venv/bin`, `.venv/pyvenv.cfg` and the venv `site-packages` is taken before and after each verification. Any
  change fails the run (`TAMPER: ...`), is stored in `verifications.tamper_json` and raises a `tamper` event. Child
  runs use a fresh per-run copy of the checkout.
- **Untrusted output (R1):** child stdout/stderr are read with hard caps (1 MB per stream kept, rest drained and
  dropped; 1 MB per protocol line; 16 MB per protocol session). JSON from a child is parsed with every failure mode
  caught (oversize, invalid UTF-8, malformed, NaN/Infinity, RecursionError, MemoryError) and fails closed.

**What the sandbox does NOT protect against (stated plainly):**
- It does not hide the read-only `/usr` runtime: candidate code can read any world-readable system file there.
- It shares the host kernel: a kernel or bubblewrap vulnerability (namespace escape) defeats it. No seccomp filter
  and no Landlock layer is installed (Landlock is available on this kernel but not used; no fallback mechanism is
  implemented, bwrap missing means fail closed).
- CPU time is bounded only by the profile timeout; memory by RLIMIT_AS, **except in the one oracle run that has no
  RLIMIT_AS** (deliberate, round 5): there a candidate can use host memory until the timeout or the kernel OOM
  killer stops it. Disk writes are bounded by RLIMIT_FSIZE per file into the private copy/tmpfs (scratch disk use
  under the system temp dir is possible up to those limits). There is no cgroup memory limit.
- It does not stop **same-account Bots** (the first threat model above): they run outside any sandbox.
- The integrity tripwire detects changes; it does not prevent them (the sandbox does). A legitimate concurrent edit
  of the code/config during a verification also trips it (fail closed by design).
- Verification of behaviour remains testing: the oracle only checks the functions defined in `oracles.py` (see
  "Oracle coverage" below for exactly which inputs), and the advisory unittest child only runs the declared tests.

### H1: harness detection — user decided: NOT accepted; mitigated by (a) neutral harness, (b) varied runs, (c) pure-function rule
Momus's H1: a candidate whose `add` returns `a+b` only when `PYTHONSAFEPATH` is set and `HOME` contains `verify-`
(i.e. only inside the old harness) and `a-b` otherwise. The user decided on 2026-11-03 that this is **not** an
accepted residual. Mitigations:
- **(a) Neutral harness.** Oracle runs bind the checkout at a random ordinary path (`/home/<user>/[code|projects/]<proj>`,
  `/srv/<proj>`, `/opt/<proj>`), with HOME=`/home/<user>`, a matching `/etc/passwd` entry and a random ordinary
  hostname; no path, variable, file or argument contains `omo`, `verif`, `oracle`, `harness`, `sandbox`, `bwrap`,
  `grok` or `probe` (asserted by tests). The candidate is imported exactly as `python3 -c 'import calc'` would:
  the program is passed with `-c` (no script path), its first statement is `import calc` (plus, in the isolated
  run only, an `sys.path.insert(0, '')` that `-I` requires), `__main__` holds only standard dunders at import time,
  and no `-I`/`-P`/`PYTHONSAFEPATH` is used in the plain run.
- **(b) Varied runs.** Every verification runs the oracle **3 times** in shuffled order, each in a fresh copy and a
  different neutral layout: *plain* (`python3`, no flags, login-like env), *isolated-flags* (`python3.13 -I -S -B`,
  minimal env with `PYTHONSAFEPATH=1` and a random `PYTHONHASHSEED`), *interactive-like* (`python3 -q -i`, different
  LANG/EDITOR/`PYTHONDONTWRITEBYTECODE`). The call-stack depth at which the candidate is called is randomized per
  call (0..300 extra frames). The runs also differ in **resource limits** (round 5): one run has a random low
  RLIMIT_AS (256-512 MiB), one the default 4 GiB, one none, so code that senses the memory limit (by allocating or by
  reading it) gets different answers in different runs. The same inputs go to every run; every run must match the
  oracle and all results must be identical across runs. A correct candidate passes all three (tested).
- **(c) Pure-function rule** (`candidate_scan.pure_module_problems`, required for every `fixture-*` profile via
  `pure_modules: {"allow_imports": [...]}`, applied to **every** `.py` under `editable_paths` in the checked-out
  revision, changed or not, so a relative import can only reach scanned code). It is an
  allowlist: module top level may contain only a docstring, allowlisted imports (calc: **none**; the ceiling for any
  profile is `math`, `operator`, `cmath`, `numbers`), function/class definitions without decorators, and immutable
  literal constants. Inside code: names must be defined in the module or be one of a fixed set of pure builtins (no
  `open`, `print`, `getattr`, `type`, `vars`, `__import__`, ...); no dunder names, no `_private`/dunder attributes,
  no `.format`/`.format_map`, no `global`/`nonlocal`, no mutable defaults, no attribute/item assignment on
  module-level objects, no `with`/async, no imports inside functions. Round 5 additions (after HMEM):
  - **no `try` statement at all** (`try/except/else/finally`, `except*`): an exception raised by a resource probe
    (MemoryError, RecursionError, ...) can never be turned into a value. Round 4 banned only broad handlers, and
    `return` inside `finally` swallowed the MemoryError anyway (HMEM). The broad-handler list is kept as a redundant
    second layer;
  - `bytes` removed from the pure builtins (`bytearray`/`memoryview` were never in it); huge allocations through
    other builtins (`[0] * n`, `range` materialisation) can only raise, which with no `try` is an error, not a value;
  - no special-method definitions except `__init__` (no `__del__`, `__exit__`/`__enter__`, `__eq__`, `__index__`,
    ...; class-body `__del__ = f` is a dunder name and also rejected), so no implicit hook can run code on an error
    or at collection time;
  - no frame/code/generator/coroutine/traceback internals (`gi_*`, `cr_*`, `ag_*`, `f_*`, `co_*`, `tb_*`, `mro`,
    `throw`, `close`, `with_traceback`, plus the static scan's introspection list);
  - **relative imports** (level > 0) are allowed only when they resolve to a `.py` file under the editable paths
    (which is itself scanned by this rule); `from . import x` must name such a module, `from .m import x` needs `m` to
    be one; anything else (protected `tests/`, outside the package, a missing module, `*`) is rejected. Absolute
    imports of the candidate's own package go through the normal allowlist (rejected for calc).
  That forbids, among others, `os.*`, `sys.*`, `resource`, environment, flags, argv, cwd, pids, users, platform,
  socket, time, random, threading, signal, inspect, traceback, file I/O and `/proc`.

**Which variants each layer stops** (`tests/integration/test_harness_detection.py`; every variant fails the full
verifier):
- Defeated by (a)+(b) even with every static check disabled: Momus's H1 payload; keyed on `sys.flags.isolated`,
  `sys.flags.no_site`, *not* isolated; `PYTHONSAFEPATH` present or absent; cwd markers; HOME markers; HOME==cwd;
  script `argv[0]`/`orig_argv`; `argv[0] != '-c'`; `sys.executable` name; `/proc/self/cmdline` markers; user name;
  `LANG`; **the RLIMIT_AS value** (`resource.getrlimit`).
- **Memory-limit probing by allocation (Momus HMEM, round 5)** is stopped by **both** (c) and (b): (c) rejects the
  `try` that would turn the MemoryError into a value (and `bytes`), and with the static rule disabled the run without
  RLIMIT_AS can make the 5 GiB allocation succeed while the limited runs cannot, so the candidate answers wrongly in
  that run and the oracle fails (`tests/integration/test_round5_probes.py`, including the relative-import helper
  variant). Round 4 of this document wrongly listed "the RLIMIT_AS value" as stopped only by (c), while (c) did not
  in fact stop HMEM; that claim is withdrawn. Caveat for (b) alone: a probe threshold above what *this host* can
  allocate fails in every run and in most normal runs too, so it does not distinguish them here; on a host that can
  allocate more, (b) alone would not see the difference. (c) does not depend on that.
- Recursion-depth probing (`try`/`finally` around deep recursion): stopped by (c) (no `try`). Without `try`, a
  RecursionError can only propagate, which fails the oracle. (b) varies the call depth but is not claimed to stop
  it on its own.
- Stopped **only** by the static pure-function rule (c): conditions every sandboxed run shares and a normal run
  lacks — `/proc/1/cmdline` is the bwrap init (it also shows host staging paths), `/workspace` does not exist,
  protocol modules (`json`) are already in `sys.modules` at call time. Others in this class: no network, the pid
  namespace, `/proc/self/mountinfo`, stdin/stdout being pipes, `os.getpid()` values. A test asserts that the three
  tested variants pass the runtime layer with the scan disabled, so this statement stays true.
- Round-5 audit of other exception-swallowing / sensing constructs (all rejected by (c), tested): `with` over a
  context manager whose `__exit__` returns True; generator `try/finally` and `close()`/`throw()`; `__del__` (def or
  class-body assignment); other dunder hooks; `try/except ValueError` and `try/else`; `except*`; `bytes`/
  `bytearray`/`memoryview`; `gi_frame.f_code.co_filename` (would reveal the path); `int.mro()`; relative imports
  of protected or missing modules; `from .m import *`; absolute self-imports. Not closed by a rule and stated here:
  `repr()`/`str()` of a function or object exposes its memory address (ASLR-randomised, the same inside and outside
  the sandbox, so it is randomness, not a harness signal; results that depend on it differ between the runs) and
  string-set iteration order follows the per-process hash seed (also random in every run).
- **Oracle coverage (round 5, after HDIST).** Every verification sends: the exhaustive grid of all argument pairs in
  [-16, 16]^2 (1089 per binary function, filtered by each function's domain predicate, e.g. no zero divisor);
  pairwise boundary values (0, +-1, +-2, +-16, +-17, both sides of 2**31, 2**32, 2**53+1, 2**63, 2**64, +-10**18,
  +-2**200, +-2**1000), and each boundary against 0, +-1, +-2, +-16 in both orders; then `cases_per_function` fresh
  CSPRNG draws (bands: |x|<=100, <=1000, <=10**12, <=2**63, <=2**200). Order is shuffled per run. Momus's HDIST
  (wrong for |a|,|b|<=10 except the unittest pairs) now fails every time (tested 5x plus the manual repro).
  **Limit:** outside the grid and the boundary set the oracle samples. A function wrong only on a sparse set of
  inputs that are neither in the grid, nor boundaries, nor likely random draws (e.g. one specific 40-digit value, or
  |a| in 17..99 with a rare pattern) can still pass; review and QA are the backstop. The grid is exhaustive only for
  small integers and only for the functions defined in `oracles.py`.
- Residual (all layers): the pure-function rule applies to fixture profiles only. Real-repo profiles have no oracle
  (`cooperative`), so harness detection there is backstopped only by the required independent code review and QA.

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
  paths. Missing or template (`OMO_VERIFY_TEMPLATE`) profiles fail closed. Every process that runs candidate code
  is sandboxed (section above). Candidates are rejected before execution when they touch paths outside the
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
    - **The parent verifier never imports candidate code.** Every candidate import happens in a sandboxed child
      with a scrubbed environment (the unittest child: `-I -S -B` with PATH, LC_ALL, HOME and the profile's
      `env_allow`/`env_set`; oracle runs: the neutral, varied environments of H1; never any `OMO_*`). No nonce, result path or other secret is passed
      in env, argv or fds, so there is nothing for N1/N2/N4 to steal. Everything a child says is untrusted.
    - **ORACLE mode (assurance `oracle`).** `omo_grok/oracles.py` (trusted tree) builds an exhaustive small grid,
      boundary pairs and fresh SystemRandom inputs for every verification (see "Oracle coverage" above; shuffled). `omo_grok/oracle_child.py` builds the `-c` program that imports the
      candidate and must print `{"ready":true}`; the parent then sends **one** request at a time over stdin and generates the next only
      after the answer arrives. Each answer must be a raw JSON value; the parent checks `type(v) is int` (bool and
      float never pass) and equality with its own oracle value. The child must answer every case and exit 0. Exiting
      early, printing `VERIFIED`, forging stdout, always-equal objects and constant answers all fail, with or
      without the static scan (regression tests). An oracle is **required** for every `fixture-*` profile, at config
      load and at submit.
    - **Advisory unittest child.** `omo_grok/unittest_child.py` runs the `expected_tests` manifest in a separate
      child and prints one `OMO-UNITTEST-REPORT` line. It still snapshots test machinery, builtins and key stdlib
      functions as a tripwire (round-2 E). For oracle profiles the report is advisory: a missing or negative report
      fails the run, and a positive report alone does not make it pass (the oracle decides).
    - **Cooperative assurance.** A Python profile without an oracle, and every `exit_code_only` profile, is
      `cooperative`: its pass is forgeable by a malicious candidate. The detail line starts
      `assurance=cooperative: ... forgeable`, every verification record stores `assurance`, and the final gate
      requires `verifier_assurance_recorded` plus an accepted `code_review` **and** `qa` on the same revision
      (`cooperative_verifier_backed_by_code_review_and_qa`), and `independent_code_review_and_qa` (below). The gate's assurance string carries
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
    - The oracle is black-box testing. Harness detection (H1) is mitigated as described above, not made
      impossible: the sandbox itself is observable, and for the STATIC_ONLY class only the static pure-function
      rule stops code that keys on it. That rule covers fixture profiles only. Outside the small grid and the
      boundary set the oracle only samples (see "Oracle coverage").
    - The oracle covers only the functions defined in `oracles.py` (today `calc.add`, `calc.mul` for `calc-arith`).
    - Profiles for real repos without an oracle are `cooperative`: the verifier result is forgeable, and only the
      required `code_review` + `qa` on the same revision, from Bots outside the executor's independence group,
      stand behind it.

## Independence of code review and QA (enforced)
The final gate check `independent_code_review_and_qa` requires, for the exact candidate revision, the latest
accepted `code_review` **and** the latest accepted `qa` to come from Bots whose bot id **and** independence group
differ from the executor's (the code reviewer and the QA Bot may be the same Bot). Reviews on an older revision never count
(test: `test_review_on_an_older_revision_does_not_satisfy_the_gate`). This applies in every mode, fixture included.

Reduced assurance (non-independent review/QA) is accepted **only** when the approval for that job carries
`accept_reduced_assurance=true`. It is never a config default (the old policy keys `require_independent_assurance`
and `fixture_allow_reduced_assurance` were removed; a config that still sets them is rejected). Such a job's gate
assurance starts with `REDUCED_ASSURANCE_ACCEPTED_BY_APPROVAL+`, `omoctl status` prints `!!! WARNING !!!` and
`!!! REDUCED ASSURANCE !!!` on the gate line, and the export carries an `ASSURANCE_LABEL` of `REDUCED ASSURANCE`.
"Independent" here means separate Bots on the same account: they share this computer, so this is separation of
duties between cooperating agents, not protection against a malicious same-account Bot.
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
