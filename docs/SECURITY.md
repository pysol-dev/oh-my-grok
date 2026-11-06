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
- **Namespace self-check (round 6).** A version string proves nothing (Momus: a fake `bwrap` script that prints the
  version and execs the command passed the round-5 probe). `sandbox.namespace_self_check` now runs a tiny Python
  probe through the real `bwrap` argument set with a random nonce and requires: pid 1 inside is the bwrap init
  (`/proc/1/comm == "bwrap"`, own pid != 1 and != the host pid), the only network interface is `lo`, a fresh sentinel
  file in the host `/tmp` and the `omo_grok/` package directory are **not** visible, `/proc/self/uid_map` is a single
  one-id mapping with our uid inside (not the host's `0 0 4294967295`), and the nonce comes back. Any mismatch, a
  missing report or a missing nonce fails closed (tests: an exec-through shim and a canned-JSON shim are rejected).
  **Limit:** a hostile replacement binary written against this check can fake these answers. The check catches a
  wrong or broken binary, not a deliberate same-account adversary. **Disclosure:** Momus reports that the box user
  has passwordless sudo (this build never invokes sudo and did not verify it). If so, any same-account process can
  replace `/usr/bin/bwrap` or the system Python, so the same-account threat model above (cooperating Bots, not
  adversaries) covers the sandbox binary too.
- **Scratch cleanup** (`verifier._rmtree`, rewritten in round 6) runs after every sandboxed process is dead. It never
  follows a symlink: entries are classified with lstat relative to their parent directory fd; symlinks and files are
  unlinked; a directory is first made owner-rwx with `fchmodat(dir_fd, name, AT_SYMLINK_NOFOLLOW)` after re-checking
  that the name is still the same directory inode (so modes 000 / 0o100 no longer block it), then **renamed**
  (dir-fd relative, never follows symlinks) up to the top of the tree and processed there. The walk is iterative and
  flat (O(1) open fds, no recursion), so trees deeper than the old 200-level cap are removed; it is bounded by
  `RMTREE_MAX_ENTRIES` (1,000,000 entries). **Residual:** it returns the number of entries it could not remove;
  if that is non-zero (e.g. more than the bound, or an entry it could not change), the leftover is NOT deleted and
  the verification's stderr says `scratch cleanup incomplete: N entries left in <dir>` (pass/fail is unaffected).
  Round 5 claimed cleanup without saying that 000-mode and >200-deep trees leaked; that was wrong. Tests:
  000/0o100 dirs, a 2500-deep tree, symlinks to a read-only outside directory (untouched), and the bound.
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
  run only, an `sys.path.insert(0, '')` that `-P` requires), `__main__` holds only standard dunders at import time,
  and no `-I`/`-P`/`PYTHONSAFEPATH` is used in the plain run.
- **(b) Varied runs.** Every verification runs the oracle **3 times** in shuffled order, each in a fresh copy and a
  different neutral layout: *plain* (`python3`, no flags, login-like env, no `PYTHONHASHSEED`, so the interpreter
  draws its own random seed), *isolated-flags* (`python3.13 -s -S -B -P`, minimal env with an explicit random
  `PYTHONHASHSEED`), *interactive-like* (`python3 -q -i`, different LANG/EDITOR/`PYTHONDONTWRITEBYTECODE` and a second,
  different explicit `PYTHONHASHSEED`). **Correction (round 6):** rounds 4-5 used `-I` with `PYTHONSAFEPATH=1` and a
  `PYTHONHASHSEED` in the isolated run and this document called that seed random; `-I` implies `-E`, so both
  variables were silently ignored (the run used an ordinary random seed and `-I` alone provided safe-path). The
  isolated run now uses `-s -S -B -P` (user site off, no site, no bytecode, safe path; no `-E`), so its pinned seed
  takes effect; a test checks that each run's flags + env give a distinct, effective `hash()`. The call-stack depth
  at which the candidate is called is randomized per call (0..300 extra frames; 0 in the plain run).
  **Recursion headroom (round 7, C2-4).** Every candidate call is made from the `-c` program's top level, exactly
  like a script calling `calc.add(...)` (the protocol loop is a generator that hands each call to a top-level
  loop). An unpadded call (always in the plain run, and whenever a padded run draws 0) runs at the **stock**
  recursion limit, at the same frame depth as real use. A padded call raises the limit, for that call only, by
  exactly the number of frames counted below the callee (walked via the frame chain at the call, not estimated),
  and restores it afterwards. So the callee never has more headroom than a top-level call in a normal process,
  and correct deep code is not starved by the padding. **Correction:** round 6 said the child "raises its recursion
  limit by the padding (+50)"; it raised it once to stock + pad_max + 50 for the whole session and in every run,
  including the plain one with no padding, so code recursing ~1010 deep passed the oracle but raises RecursionError
  in real use (Momus C2-4). Tests: in all three flag sets and with padding 0, 150 and 300, the largest depth a real
  top-level call can reach (measured, 997 here) succeeds and one more raises RecursionError; C2-4 fails the
  verifier with the scan on and off; a correct depth-700 recursive `add` passes. The runs also differ in **resource limits** (round 5): one run has a random low
  RLIMIT_AS (256-512 MiB), one the default 4 GiB, one none, so code that senses the memory limit (by allocating or by
  reading it) gets different answers in different runs. The same inputs go to every run; every run must match the
  oracle and all results must be identical across runs. A correct candidate passes all three (tested).
- **(c) Pure-function rule** (`candidate_scan.pure_module_problems`, required for every `fixture-*` profile via
  `pure_modules: {"allow_imports": [...]}`, applied to **every** `.py` under `editable_paths` in the checked-out
  revision, changed or not, so a relative import can only reach scanned code). **Round 7: a real allowlist.**
  Rounds 4-6 called this rule an allowlist, but only names and imports were allowlisted; node types and attribute
  access were governed by bans, and each round found another construct outside the bans (`try`, `is`, sets, then
  dict views: C2-3). That description was inaccurate. `candidate_scan._allowlist_problems` now accepts ONLY:
  - module level: a docstring; `import m` / `from m import name` where `m` is in the profile's `allow_imports`
    (calc: **none**; ceiling `math`, `operator`, `cmath`, `numbers`) and `name` is in `PURE_MODULE_ATTRS[m]`;
    `from __future__ import annotations`; relative imports of scanned local modules; `NAME = <literal>` (int,
    float, str, bool, None, or tuples of them); top-level `def`s. No classes, no other statements;
  - functions: plain positional parameters only (no `*args`, keyword-only or `**kwargs`, no decorators), literal
    defaults, annotations only `int`/`float`/`bool`/`str`/`None`; no nested functions or lambdas;
  - statements: `return`, assignment / augmented assignment to local names (or a tuple/list of names), `if`,
    `while`, `for <name(s)> in range(...)`, `break`/`continue` inside loops, `pass`, a docstring, and
    `raise ValueError|TypeError|ZeroDivisionError|ArithmeticError|OverflowError[(...)]` (no `from`). Expression
    statements, `assert` (behaves differently under `-O`), `del`, `try`, `with`, `match`, `global`, `yield` etc.
    are rejected;
  - expressions: `+ - * / // % ** << >> | ^ &`, unary `+ - not ~`, `and`/`or`, comparisons `== != < <= > >= in
    not in` (**no `is` / `is not`**, not even against None), `x if c else y`, constants int/float/str/bool/None
    (no bytes, complex, Ellipsis), tuple and list displays (no starred), subscripts and slices (load only), and
    calls whose callee is a name or an allowlisted module attribute, with no `*`/`**` arguments;
  - names: parameters, locals, module-level names, and the builtins `abs min max int float bool str len range
    isinstance round divmod pow sum all any tuple list` plus the five exception types above;
  - attributes: only `<allowlisted module>.<name in PURE_MODULE_ATTRS>` (e.g. `math.isqrt`, `operator.add`) or a
    public name of a scanned local module. There is **no attribute access on values**, so no methods at all
    (`.keys()`, `.items()`, `.upper()`, `.sort()`, `.format`, `.__class__`, ...).
  **Dict and set literals, dict/set/list comprehensions, generator expressions, f-strings, walrus, lambdas and
  `dict`/`set`/`frozenset`/`sorted`/`repr`/`type`/`hash`/`id` are all outside the allowlist.** Anything not listed
  is rejected. The real calc fix (`return a + b`), the fixture base file and every earlier correct candidate pass
  (tested); the one earlier allowlist example that used a class with `self` was changed to a plain function.
  The older checks below still run as defence in depth (both layers must accept). Earlier wording, kept for the
  denylist layer: module top level may contain only a docstring, allowlisted imports, function definitions without
  decorators, and immutable literal constants. Inside code: names must be defined in the module or be one of a fixed set of pure builtins (no
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
  - (round 6) no `is` / `is not` except against the literal `None` (C2-1), and no `set`, `frozenset`, `hash`, `id`,
    set literals or set comprehensions (C2-2).
  That forbids, among others, `os.*`, `sys.*`, `resource`, environment, flags, argv, cwd, pids, users, platform,
  socket, time, random, threading, signal, inspect, traceback, file I/O and `/proc`. The static rules are
  still a tripwire, not a proof: an allowlist can only be as good as the semantics of what it admits (for
  example, `str(x)` of an int with more than 4300 digits raises in every run and in real use alike, so that is an
  error, not a probe; and anything the allowed subset can compute from its arguments alone is by construction not
  environment-dependent, apart from recursion depth. Since C2-4 the oracle gives the candidate exactly the
  headroom of a **top-level** call in a normal process; that equals real use only for a caller at top level. A
  real caller nested N frames deep has N fewer frames available, so code that passes the oracle can still hit
  RecursionError when called from deep inside another program, and code that recurses close to the limit can
  fail here but work elsewhere (see "False rejections")).

**Which variants each layer stops** (`tests/integration/test_harness_detection.py`; every variant fails the full
verifier):
- Defeated by (a)+(b) even with every static check disabled: Momus's H1 payload; keyed on `sys.flags.isolated`,
  `sys.flags.no_site`, *not* isolated; `PYTHONSAFEPATH` present or absent; cwd markers; HOME markers; HOME==cwd;
  script `argv[0]`/`orig_argv`; `argv[0] != '-c'`; `sys.executable` name; `/proc/self/cmdline` markers; user name;
  `LANG`; **the RLIMIT_AS value** (`resource.getrlimit`); (round 6) `sys.flags.safe_path`, `not
  sys.flags.no_user_site`, `PYTHONHASHSEED` present or absent; argument identity (C2-1, via the aliased cases).
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
  the sandbox, so it is randomness, not a harness signal). **Correction (round 6):** this paragraph used to say
  that string-set iteration order "follows the per-process hash seed (also random in every run)", implying runs
  would catch hash-dependent code. That was wrong twice: such behaviour differs between runs only *probabilistically*
  (Momus's C2-2 passed 8 of 12 times), and the isolated run's pinned seed was ignored under `-I`. Hash-order
  dependence is enforced statically only since round 7 (C2-3 below); the per-run seeds are a second,
  probabilistic layer.
- **C2-1: argument identity (Momus re-review #6).** `if a is b and a > 1000: return a - b` passed every case,
  because the child decodes every request with `json.loads`, so the two arguments were always distinct objects,
  while a real caller writing `x = 5000; add(x, x)` passes one object. Variant: `a is b and (a > 256 or a < -5)`
  (outside CPython's small-int cache). Fixes: (c) rejected `is` / `is not` unless one side is the literal `None` (round 7: `is` is outside the allowlist entirely);
  and the oracle sends **aliased** cases: every equal-argument case (grid and boundary diagonals, plus random equal
  pairs, including values far outside the small-int cache) is sent both as fresh objects and with the child passing
  the same object twice (`"alias": [[0, 1]]`), positionally and by keyword. Both variants fail with the static rule
  on and, with it disabled, fail the oracle (tests in `tests/integration/test_round6_probes.py`).
- **C2-2: hash-seed order (Momus re-review #6).** `o = list({'alpha','beta','gamma','delta'}); a - b if o ==
  sorted(o) else a + b` passed 8/12. Fix: (c) bans `set`, `frozenset`, `hash`, `id`, set literals and set
  comprehensions in pure modules (rejected statically every time; tested with literal, `set()`, `frozenset()`,
  comprehension and `hash()` variants). The per-run seeds (b) remain, but they detect hash dependence only with some
  probability. **Correction (round 7):** round 6 said hash-order dependence was thereby "enforced statically" and
  that dicts "stay allowed" because dict iteration is insertion-ordered. The second half is true of iterating a
  dict, but dict **views** support set operators (`d.keys() | ()`, `d.keys() & d.keys()`, `d.keys() ^ {}`,
  `d.items() | ()`) that return real `set`s, whose order follows the hash seed. Momus's C2-3 passed 10 of 11 runs
  under round 6, so the claim was false.
- **C2-3: dict-view set operations (Momus re-review #7).** `o = list({'alpha':0,...}.keys() | ())` then
  `a-b if o==sorted(o) else a+b`, plus the variants `d.keys() & d.keys()`, `d.keys() ^ {}` and `d.items() | ()`.
  Closed by the round-7 allowlist (c): dict literals, dict comprehensions and every attribute access on a value are
  outside it, so all four are rejected before running (tests and repro). With the static rule disabled the runtime
  layer catches them only probabilistically (see the repro numbers in `spec/PROGRESS.md`); the allowlist is the
  enforcement.
- **Oracle-vs-real-call audit (round 6).** Ways the oracle's calls could differ from a real caller's, and status:
  - *identity*: covered (C2-1, rule + aliased cases). Identity of unequal values cannot be observed without `is`/`id`.
  - *keyword vs positional*: covered. Every grid/boundary case is also sent with all arguments by keyword, the grid
    again in mixed form (`f(a_val, b=b_val)`), and random draws in a random form. This makes the parameter names
    (`a`, `b`, `OracleFn.params`) part of the contract: a correct `add(x, y)` now fails.
  - *bool vs int*: covered for `calc.add`/`calc.mul` (`accepts_bool`): `True`/`False` against each other and against
    0, +-1, 2, 2**63, -2**200, both orders and by keyword; the expected value is the int result (`True + True == 2`),
    and the answer must still be exactly `int`.
  - *call count / order state*: each run gets the cases in a fresh shuffled order, one at a time; the three runs see
    different orders, and pure modules cannot keep state across calls except in module-level objects, which (c) makes
    immutable (no attribute/item assignment, no `global`). Not separately tested beyond that.
  - **Not covered (disclosed):** `int` subclasses, `float`, `Decimal`, `Fraction`, strings or other types are never
    sent (JSON carries only exact ints; bools are sent by the child as JSON `true`/`false`). Code that misbehaves only
    for an `int` subclass or a non-int argument passes the oracle; for `calc` the declared domain is int (and bool),
    and (c) plus review/QA are the backstop.
- **Oracle coverage (round 5, after HDIST).** Every verification sends: the exhaustive grid of all argument pairs in
  [-16, 16]^2 (1089 per binary function, filtered by each function's domain predicate, e.g. no zero divisor);
  pairwise boundary values (0, +-1, +-2, +-16, +-17, both sides of 2**31, 2**32, 2**53+1, 2**63, 2**64, +-10**18,
  +-2**200, +-2**1000; round 7: +-2**300, +-(2**300+1), +-(2**1000+-1), +-(2**2048+12345), +-3**1500,
  +-(2**4096-1), +-2**4096, +-(2**4096+1)), and each boundary against 0, +-1, +-2, +-16 in both orders; then
  `cases_per_function` fresh CSPRNG draws spread over six bands: |x|<=100, <=1000, <=10**12, <=2**63, <=2**200, and
  (round 7) |x| in [2**k, 2**(k+1)) with k uniform in 0..4096 and a random mantissa, random sign. Round 6 adds
  keyword, mixed and aliased copies and bool cases (C2-1 and the audit above); with the round-7 boundaries that is
  about 15,700 shared cases per run (47,000 per verification), plus since round 8 100 per-run large-band cases (below); a correct candidate's verification takes about 3.5-4 s here. **Magnitudes covered:** exhaustively
  [-16, 16]; at fixed boundaries up to 2**4096+1; by sampling, every bit length 1..4097 has the same chance per
  large-band draw.
  **Correction (round 8, Momus re-review #8).** Round 7 said "about 16 large-band draws per function per run, so any
  range of bit lengths wider than a few hundred is hit with near certainty". That was false: the 16 counted both
  arguments (about 8-10 per argument per verification), and all 3 runs reused the same inputs, so they added no
  draws. Momus measured `return a - b if 2**2500 <= abs(a) < 2**2800 else a + b` passing 10/20 verifications, a
  500-bit band 6/20 and a 1000-bit band 1/20. The three round-7 large-magnitude tests ("wrong for |a| > 2**300
  except 2**1000", "wrong for 2**2049 < |b| < 2**4095", "wrong above 2**4000") fail because their ranges contain
  **fixed boundary values**, not because of sampling, so they did not support the claim. Boundary values (|x|)
  inside each range, checked by computation (round 9 correction: round 8 wrongly listed 2**4096-1 for the second
  range): |a| > 2**300 except 2**1000: 2**300+1, 2**1000-1, 2**1000+1, 2**2048+12345, 3**1500, 2**4096-1,
  2**4096, 2**4096+1; 2**2049 < |b| < 2**4095: **only 3**1500** (bit length 2378; 2**2048+12345 is below 2**2049
  and 2**4096-1 is above 2**4095); |a| > 2**4000: 2**4096-1, 2**4096, 2**4096+1.
  **Large-band sampling since round 8.** Each of the 3 oracle runs additionally gets its **own** fresh cases, drawn
  for that run only and never sent to another run (`oracles.run_cases`, `LARGE_PER_RUN = 50`): per function, 50
  calls whose every argument is an independent draw with exponent k uniform in 0..4096 (bit length k+1), random
  mantissa and sign, in a random call form. That is exactly **50 x 3 = 150 independent large-band draws per
  argument position per verification**, in addition to the shared random draws (each shared draw is large-band with
  probability 1/6: about 8 per argument per verification, not counted below). For a candidate that is wrong exactly
  when one argument's bit length lies in a set of W of the 4097 possible values, the per-verification catch
  probability from the dedicated draws alone is **P = 1 - (1 - W/4097)^150**: W=50: 0.841; W=100: 0.9754;
  W=300: 0.999989 (miss 1.1e-5); W=500: 1 - 3.3e-9; W=1000: 1 - 6e-19. (`oracles.large_band_catch_rate`; a test
  checks W=300 >= 0.999.) **Measured** (20 verifications each, `return a - b if 2**lo <= abs(a) < 2**hi else a +
  b` plus a correct `mul`, bands chosen to contain no fixed boundary): Momus's 2**2500..2**2800 (W=300): 0/20
  passed; 500-bit band 2**3100..2**3600: 0/20; 1000-bit band 2**2400..2**3400: 0/20; also W=100
  (2**2500..2**2600): 0/20 and W=50 (2**2500..2**2550): 1/20 passed (expected about 0.16 x shared-draw misses).
  Cost: 100 extra cases per run; a correct candidate's full verification (unittest child + 3 oracle runs) took
  3.5 s here.
  **Which tests rely on what.** Fixed boundaries (not sampling): the three round-7 large-magnitude tests in
  `test_round7_allowlist.py`, HDIST and the grid tests. Sampling only: `test_round8_sampling.py::
  test_wrong_only_in_a_300_bit_band_fails_by_sampling_alone` (bands 2**2500..2**2800 on `a` and 2**3100..2**3400
  on `b`; a companion test asserts no fixed boundary, grid value or non-random case has a bit length in either
  band), and `test_each_run_gets_its_own_large_draws` (ids and values of the per-run draws are disjoint across
  runs; the shared cases are identical). The sampling tests can fail spuriously with probability about 1e-5 each. **Not covered:** nothing above about 2**4097 is
  ever sent (products must stay under Python's default 4300-digit int-to-str limit that the JSON protocol needs),
  so code wrong only above that passes; and within the large range, a narrow band of bit lengths or a sparse
  pattern of values is only sampled (rates below). Order is shuffled per run. Momus's HDIST
  (wrong for |a|,|b|<=10 except the unittest pairs) now fails every time (tested 5x plus the manual repro).
  **Limit:** outside the grid and the boundary set the oracle samples. A function wrong only on a sparse set of
  inputs that are neither in the grid, nor boundaries, nor likely random draws (e.g. one specific 40-digit value, or
  |a| in 17..99 with a rare pattern) can still pass; review and QA are the backstop. The grid is exhaustive only for
  small integers and only for the functions defined in `oracles.py`.
- **False rejections and failures of correct code (disclosed, round 8).** The allowlist and the oracle reject some
  correct code; these are deliberate costs, not bugs:
  - f-strings (`f"{a}"`), `str.format` and every method call on a value (`s.upper()`, `x.bit_length()`), plus
    dict/set literals, comprehensions, classes, lambdas, `assert`, `try` and `is None`;
  - `if __name__ == "__main__":` at module level (reads the dunder `__name__`; a module-level `if` is not
    allowlisted). Not allowed even though the body would not run on import: excluding the body from the scan would
    create an unscanned region, and scanning it would reject typical bodies (`print(...)`) anyway;
  - `__all__` **is allowed since round 8**, but only as `__all__ = (...)` or `[...]` of string constants at module
    level (it is never readable by the code: any load of `__all__` is still a dunder name and rejected; annotated,
    augmented and chained forms are rejected; tested);
  - recursion close to the limit: a correct `add` that always recurses about 990 deep fails, because the advisory
    unittest child calls it from deeper in its stack (unittest's own frames); measured here: 985 passes, 990 fails
    (`tests not passed`), 970 passes (tested). The oracle itself allows a top-level call up to the stock depth
    (997 frames).
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
  through `omoctl hook set ROLE` using a hidden `getpass` prompt. They never go in argv, shell history, chat (one exception: see the 2026-11-04 disclosure below), README,
  commits or test logs.
- This file mode prevents **accidental disclosure** only. Any same-account Bot can still read it.
- `hook check --probe` performs a TLS handshake only. It never POSTs and never prints the URL path or the key.
- Never put login tokens, OAuth or session material, or webhook secrets in agent prompts, routine instructions,
  repository commits or routine outputs.
- Rotation: run `omoctl hook set ROLE` again with the new value. Rotate the key in the routine UI first. A changed key
  shows up as `HTTP 401/403: webhook key rejected` and blocks the task until resumed.
- **2026-11-04 disclosure (honest record):** the terminal path (`hook set`: GUI paste delivered length=0) and the
  first form attempt (`hook serve` with `Referrer-Policy: no-referrer`, so Chrome sent `Origin: null` and got 403)
  both failed. The user then chose to paste all 7 webhook URL/key pairs into the Hephaestus chat, and Hephaestus stored
  them. Those keys therefore exist in that chat's history (and any transcript copies), outside `hooks.json`. Treat them
  as exposed: **rotation of all 7 is recommended** (RUNBOOK "Rotation"). Until rotated, anyone who can read that chat
  history holds the routine webhook keys and can POST **arbitrary text into that routine's prompt** (prompt-injection
  and usage-cost risk). Engine-sent wake-ups carry only ids (job/task/dispatch/attempt); claiming still needs the
  engine's dispatch_id match, and the plan gate, approvals and final gate still apply. A leaked-key POST is not limited
  to those ids.

### `omoctl hook serve` (local masked form)
- Binds `127.0.0.1` only, random port by default. Only `/setup/<token>` is served (404 otherwise, constant-time compare).
- Token: **per-run session token**, 256 bits (`secrets.token_urlsafe(32)`). Valid for the whole server lifetime
  (reusable across requests in that run), new each run, not single-use.
- The tokenized URL goes only to a 0600 URL file (written atomically). It is deleted on Finish, idle timeout,
  Ctrl-C (SIGINT), or SIGTERM (the serve loop installs handlers so cleanup runs). It is not printed unless
  `--print-url` is given. The RUNBOOK redirect log (`state/logs/hook_serve.log`, or the legacy `/tmp/omo_hook_serve.log`)
  is removed by the same cleanup paths when present.
- Host must be exactly `127.0.0.1:<port>`, and Origin is either absent or exactly `http://127.0.0.1:<port>`
  (`null` refused), as a DNS-rebinding/CSRF guard.
- Headers: `Cache-Control: no-store`, strict CSP (`default-src 'none'`, `form-action 'self'`, `frame-ancestors
  'none'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`.
- POST body is urlencoded only, capped at 64 KB and 64 fields (over that gives 400). Malformed URLs give a per-role error
  with a value-free diagnostic. No request logging.
- Idle timeout 30 min (`--idle-minutes`), plus a Finish button. SIGTERM/SIGINT stop the server the same way.
- **Residuals:** any same-account process can read the URL file or connect to loopback, so this is not a boundary
  against same-account code. The token is reusable until stop. Values sit in browser memory and form state until the
  tab closes. It is plain HTTP on loopback. The box browser's own history may record the tokenized URL (harmless once
  the server stops).

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
