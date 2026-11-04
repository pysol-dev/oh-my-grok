"""Static, pre-execution AST scan of candidate source files (fail closed).

Rejects constructs that let code under test subvert an in-process test runner instead of fixing the code: process
exits, interpreter/runtime introspection and patching, dynamic code execution, and mutation of other modules or
classes. Per-profile `static_scan` may ALLOW specific names (allow_imports / allow_attrs / allow_calls) or add
dunder overrides to forbid; scanning itself cannot be switched off. Unparseable or non-UTF-8 source is rejected.

This is a tripwire, not a sandbox: Python is too dynamic for a static scan to be complete (see docs/SECURITY.md)."""
from __future__ import annotations

import ast

FORBIDDEN_IMPORTS = {"ctypes", "importlib", "gc", "inspect", "unittest", "builtins", "__builtin__", "atexit", "signal",
                     "threading", "_thread", "multiprocessing", "subprocess", "faulthandler", "code", "codeop", "runpy",
                     "marshal", "pickle", "types", "sys", "site", "pytest", "_pytest", "trace", "pdb", "bdb",
                     "dis", "weakref", "copyreg", "posix", "nt", "io", "_io", "tracemalloc", "concurrent", "asyncio",
                     "mmap", "resource", "fcntl", "pty", "socket", "shutil", "tempfile", "pathlib"}
FORBIDDEN_ATTRS = {("os", "_exit"), ("os", "abort"), ("os", "kill"), ("os", "killpg"), ("os", "fork"),
                   ("os", "forkpty"), ("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execve"),
                   ("os", "execl"), ("os", "execlp"), ("os", "execvp"), ("os", "execvpe"), ("os", "spawnv"),
                   ("os", "posix_spawn"), ("os", "write"), ("os", "open"), ("os", "dup2"), ("os", "environ"),
                   ("os", "putenv"), ("os", "unlink"), ("os", "remove"), ("os", "replace"), ("os", "rename"),
                   ("sys", "exit"), ("sys", "modules"), ("sys", "_getframe"), ("sys", "settrace"),
                   ("sys", "setprofile"), ("sys", "meta_path"), ("sys", "path_hooks"), ("sys", "path"),
                   ("sys", "excepthook"), ("sys", "addaudithook"), ("sys", "stdout"), ("sys", "stderr")}
FORBIDDEN_CALLS = {"exec", "eval", "compile", "__import__", "setattr", "delattr", "globals", "locals", "vars",
                   "exit", "quit", "breakpoint", "open"}
FORBIDDEN_NAMES = {"__builtins__", "SystemExit", "KeyboardInterrupt", "GeneratorExit", "__import__", "__loader__",
                   "__spec__"}
FORBIDDEN_DUNDER_ATTRS = {"__globals__", "__code__", "__closure__", "__subclasses__", "__bases__", "__mro__",
                          "__class__", "__dict__", "__builtins__", "__func__", "__self__", "__defaults__",
                          "__kwdefaults__", "__getattribute__", "__setattr__", "__delattr__", "__import__",
                          "__loader__", "__spec__", "f_back", "f_globals", "f_locals", "f_builtins", "tb_frame",
                          "gi_frame", "cr_frame", "ag_frame", "f_code", "co_code", "__reduce__", "__reduce_ex__",
                          "__traceback__", "tb_next", "f_trace", "f_lineno", "gi_code", "cr_code", "_getframe",
                          "_current_frames", "_current_exceptions", "settrace", "setprofile", "_exit", "__wrapped__",
                          "__objclass__", "func_globals"}
DEFAULT_DUNDER_OVERRIDES = {"__eq__", "__ne__", "__bool__", "__hash__", "__index__", "__int__", "__float__",
                            "__instancecheck__", "__subclasscheck__", "__class_getitem__", "__init_subclass__",
                            "__new__", "__del__", "__getattr__", "__getattribute__", "__set_name__", "__lt__",
                            "__le__", "__gt__", "__ge__"}
BUILTIN_TYPES = {"int", "float", "complex", "str", "bytes", "bool", "list", "tuple", "dict", "set", "frozenset",
                 "type", "object", "BaseException", "Exception"}


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else None


def _chain(node):
    """Full dotted chain of an attribute expression rooted at a Name: os.sys._getframe -> ['os','sys','_getframe']."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _is_str_const(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def scan_source(text: str, rel: str, rules: dict | None = None) -> list[str]:
    rules = rules or {}
    allow_imports = set(rules.get("allow_imports", []))
    allow_attrs = {tuple(a.split(".", 1)) for a in rules.get("allow_attrs", [])}
    allow_calls = set(rules.get("allow_calls", []))
    dunders = (DEFAULT_DUNDER_OVERRIDES | set(rules.get("forbid_dunder_overrides", []))) - \
        set(rules.get("allow_dunder_overrides", []))
    bad_imports = (FORBIDDEN_IMPORTS | set(rules.get("forbid_imports", []))) - allow_imports
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as e:
        return [f"{rel}: cannot parse ({type(e).__name__}); fail closed"]
    out: list[str] = []
    module_alias: dict[str, str] = {}  # local name -> imported module (top-level name)
    imported_names: set[str] = set()

    def bad(node, why):
        out.append(f"{rel}:{getattr(node, 'lineno', '?')}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in bad_imports:
                    bad(node, f"forbidden import {a.name}")
                module_alias[(a.asname or a.name).split(".")[0]] = top
                imported_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level == 0 and top in bad_imports:
                bad(node, f"forbidden import from {node.module}")
            for a in node.names:
                if a.name == "*":
                    bad(node, "star import")
                    continue
                if node.level == 0 and (top, a.name) in FORBIDDEN_ATTRS and (top, a.name) not in allow_attrs:
                    bad(node, f"forbidden import {top}.{a.name}")
                if a.name in bad_imports or a.name in FORBIDDEN_DUNDER_ATTRS:
                    bad(node, f"forbidden from-import of {a.name!r} from {node.module or '.'}")
                imported_names.add(a.asname or a.name)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDER_ATTRS:
                bad(node, f"forbidden introspection attribute .{node.attr}")
            chain = _chain(node)
            if chain:
                chain = [module_alias.get(chain[0], chain[0]), *chain[1:]]
                for i in range(len(chain) - 1):  # resolve the WHOLE chain: os.sys._getframe, os.path.os.sys, ...
                    pair = (chain[i], chain[i + 1])
                    if pair in FORBIDDEN_ATTRS and pair not in allow_attrs:
                        bad(node, f"forbidden reference {'.'.join(chain[:i + 2])}")
                    if i + 1 < len(chain) and chain[i + 1] in bad_imports and chain[0] in module_alias.values():
                        bad(node, f"chained access to forbidden module {'.'.join(chain[:i + 2])}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                "getattr", "hasattr") and node.func.id not in allow_calls:
            if len(node.args) < 2 or not _is_str_const(node.args[1]):
                bad(node, f"non-literal {node.func.id}() attribute name")
            elif node.args[1].value in FORBIDDEN_DUNDER_ATTRS or node.args[1].value in bad_imports or \
                    node.args[1].value.startswith("__"):
                bad(node, f"{node.func.id}() of forbidden attribute {node.args[1].value!r}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                bad(node, f"forbidden name {node.id}")
            if node.id in FORBIDDEN_CALLS and node.id not in allow_calls:
                bad(node, f"forbidden builtin {node.id}")
        elif isinstance(node, ast.ClassDef):
            for b in node.bases:
                if isinstance(b, ast.Name) and b.id in BUILTIN_TYPES - {"object", "Exception"} and \
                        not rules.get("allow_builtin_subclass"):
                    bad(node, f"class {node.name} subclasses builtin {b.id}")
            for kw in node.keywords:
                if kw.arg == "metaclass":
                    bad(node, f"class {node.name} sets a metaclass")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in dunders:
                    bad(item, f"class {node.name} overrides {item.name}")
                if isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name) and t.id in dunders:
                            bad(item, f"class {node.name} assigns {t.id}")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, (ast.Attribute, ast.Subscript)):
                        r = _root_name(sub)
                        if r in imported_names:
                            bad(node, f"mutates attribute of imported name {r!r} (patching another module/class)")
                            break
        elif isinstance(node, ast.Global) and rules.get("forbid_global", True):
            bad(node, "global statement")
    return sorted(set(out))


# ---------------------------------------------------------------------------------------------------------------------
# Pure-function rule (fixture profiles, `pure_modules` in the profile). The user decided (round 4, H1) that harness
# detection is NOT an accepted residual. A fixture module may compute only from its arguments: it may not observe its
# environment or context in any way (environment, flags, argv, paths, cwd, pids, users, host, time, randomness,
# files, /proc, threads, signals, stack depth, memory limits, introspection), and it may not keep state between
# calls. This is an ALLOWLIST: anything not explicitly permitted is rejected.

PURE_IMPORT_CEILING = {"math", "operator", "cmath", "numbers", "__future__"}  # the most any profile may allow
PURE_BUILTINS = {
    "abs", "all", "any", "bin", "bool", "chr", "complex", "dict", "divmod", "enumerate", "filter", "float",
    "hex", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "oct",
    "ord", "pow", "range", "repr", "reversed", "round", "slice", "sorted", "str", "sum", "super", "tuple",
    "zip", "NotImplemented", "Ellipsis",
    "ArithmeticError", "ZeroDivisionError", "OverflowError", "ValueError", "TypeError", "KeyError", "IndexError",
    "LookupError", "StopIteration", "NotImplementedError", "AssertionError", "AttributeError"}
# (round 5) `try` is banned outright in pure modules: any try/except/finally can turn a MemoryError/RecursionError
# from a probe into a value (e.g. `return` inside `finally` swallows the exception). This list is kept as a second,
# redundant layer for messages and in case the try ban is ever relaxed.
PURE_FORBIDDEN_HANDLERS = {"BaseException", "Exception", "RuntimeError", "RecursionError", "MemoryError",
                           "SystemError", "OSError", "EnvironmentError", "IOError", "ImportError",
                           "ModuleNotFoundError", "KeyboardInterrupt", "SystemExit", "GeneratorExit", "Warning",
                           "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError", "EOFError"}
PURE_FORBIDDEN_ATTRS = {"format", "format_map",  # "{0.__globals__}".format(f) reaches globals without an AST dunder
                        "mro", "with_traceback", "throw", "close"} | FORBIDDEN_DUNDER_ATTRS
# frame / code / generator / coroutine / traceback internals (gi_frame.f_code.co_filename reveals paths, f_builtins
# reaches open/__import__)
PURE_FORBIDDEN_ATTR_PREFIXES = ("gi_", "cr_", "ag_", "f_", "co_", "tb_")
# (round 6, C2-2) set/frozenset iteration order of str/bytes depends on the per-process hash seed: banned outright
PURE_FORBIDDEN_NAMES = {"set", "frozenset", "id", "hash"}
PURE_ALLOWED_DUNDER_DEFS = {"__init__"}  # __del__/__exit__/__eq__/__index__/... hooks are not allowed


def _relative_target(rel: str, level: int, module: str | None) -> str:
    """Repo-relative package path a relative import refers to (posix), e.g. ('calc/x.py', 1, 'h') -> 'calc/h'."""
    parts = rel.split("/")[:-1]  # containing package of either a module or an __init__.py
    up = level - 1
    if up > len(parts):
        return "\0outside"
    parts = parts[:len(parts) - up] if up else parts
    return "/".join([*parts, *((module or "").split(".") if module else [])])


def _local_module_ok(target: str, local_modules) -> bool:
    return f"{target}.py" in local_modules or f"{target}/__init__.py" in local_modules


def _immutable_literal(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _immutable_literal(node.operand)
    if isinstance(node, ast.Tuple):
        return all(_immutable_literal(e) for e in node.elts)
    return False


def _bound_names(tree) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            names.add(n.rest)
    return names


def pure_module_problems(text: str, rel: str, allow_imports, local_modules=frozenset()) -> list[str]:
    """Allowlist scan of one fixture source file. Returns problems (empty = pure).

    `local_modules` is the set of repo-relative .py paths that are themselves scanned by this rule (every .py under
    the profile's editable paths). A relative import (level > 0) is allowed only if it resolves to one of them;
    anything else (a protected/unscanned module, a stdlib name, outside the package) is rejected."""
    allow = set(allow_imports or []) | {"__future__"}
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as e:
        return [f"{rel}: cannot parse ({type(e).__name__}); fail closed"]
    out: list[str] = []

    def bad(node, why):
        out.append(f"{rel}:{getattr(node, 'lineno', '?')}: pure-function rule: {why}")

    module_level: set[str] = set()
    for i, st in enumerate(tree.body):
        if isinstance(st, ast.Expr) and i == 0 and _is_str_const(st.value):
            continue  # docstring
        if isinstance(st, ast.Import):
            for a in st.names:
                if a.name not in allow:
                    bad(st, f"import {a.name} not in this profile's allowlist {sorted(allow - {'__future__'})}")
                module_level.add((a.asname or a.name).split(".")[0])
        elif isinstance(st, ast.ImportFrom):
            if st.level == 0 and (st.module or "") not in allow:
                bad(st, f"import from {st.module} not in this profile's allowlist {sorted(allow - {'__future__'})}")
            elif st.level > 0:
                target = _relative_target(rel, st.level, st.module)
                for a in st.names:
                    # `from .m import x`: m must be a scanned module; `from . import m`: m itself must be one
                    ok = _local_module_ok(target, local_modules) if st.module else \
                        _local_module_ok(f"{target}/{a.name}" if target else a.name, local_modules)
                    if not ok:
                        bad(st, f"relative import {'.' * st.level}{st.module or ''} -> {a.name} does not resolve to "
                                f"a pure-scanned module under the editable paths")
            for a in st.names:
                if a.name == "*" or a.name.startswith("_") and st.module != "__future__":
                    bad(st, f"from-import of {a.name!r}")
                module_level.add(a.asname or a.name)
        elif isinstance(st, ast.FunctionDef):
            module_level.add(st.name)
        elif isinstance(st, ast.ClassDef):
            module_level.add(st.name)
            if st.keywords:
                bad(st, f"class {st.name} keywords/metaclass")
            for b in st.bases:
                if not (isinstance(b, ast.Name) and (b.id in PURE_BUILTINS and b.id[0].isupper() or b.id in module_level)):
                    bad(st, f"class {st.name} base must be a class defined above or an allowed exception type")
            for item in st.body:
                if isinstance(item, ast.FunctionDef) or (isinstance(item, ast.Expr) and _is_str_const(item.value)) \
                        or isinstance(item, ast.Pass):
                    continue
                if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None and \
                        _immutable_literal(item.value):
                    continue
                bad(item, f"class {st.name} body may contain only methods and immutable literal constants")
        elif isinstance(st, (ast.Assign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            if not all(isinstance(t, ast.Name) for t in targets) or st.value is None:
                bad(st, "module-level assignment must bind plain names")
            elif all(t.id == "__all__" for t in targets) and isinstance(st.value, (ast.List, ast.Tuple)) and \
                    all(_is_str_const(e) for e in st.value.elts):
                pass
            elif not _immutable_literal(st.value):
                bad(st, "module-level values must be immutable literal constants (no state between calls)")
            for t in targets:
                if isinstance(t, ast.Name):
                    module_level.add(t.id)
        else:
            bad(st, f"module top level may contain only allowlisted imports, defs and literal constants, "
                    f"not {type(st).__name__}")

    bound = _bound_names(tree)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.name.startswith("__") and n.name not in PURE_ALLOWED_DUNDER_DEFS:
                bad(n, f"special method {n.name} (implicit hooks such as __del__/__exit__/__eq__/__index__ are "
                       f"not allowed)")
            if n.decorator_list:
                bad(n, f"decorator on {n.name}")
            if isinstance(n, ast.AsyncFunctionDef):
                bad(n, "async function")
            if not isinstance(n, ast.ClassDef):
                for d in [*n.args.defaults, *[d for d in n.args.kw_defaults if d is not None]]:
                    if not _immutable_literal(d):
                        bad(n, f"default argument of {n.name} must be an immutable literal")
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and n not in tree.body:
            bad(n, "import outside module top level")
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bad(n, f"{type(n).__name__.lower()} statement (state between calls)")
        elif isinstance(n, (ast.With, ast.AsyncWith, ast.Await, ast.AsyncFor)):
            bad(n, f"{type(n).__name__} not allowed")
        elif isinstance(n, (ast.Try, *((ast.TryStar,) if hasattr(ast, "TryStar") else ()))):
            bad(n, "try statement not allowed (try/except/finally can turn a memory/recursion-limit probe into a "
                   "value, e.g. return inside finally)")
        elif isinstance(n, ast.ExceptHandler):
            types = [] if n.type is None else (n.type.elts if isinstance(n.type, ast.Tuple) else [n.type])
            if n.type is None:
                bad(n, "bare except (could catch RecursionError/MemoryError probes)")
            for t in types:
                if not isinstance(t, ast.Name) or t.id in PURE_FORBIDDEN_HANDLERS:
                    bad(n, f"except {ast.unparse(t)} (broad/environment-probing handler)")
        elif isinstance(n, (ast.Set, ast.SetComp)):
            bad(n, "set literal/comprehension (iteration order of str/bytes depends on the hash seed)")
        elif isinstance(n, ast.Compare) and any(isinstance(op, (ast.Is, ast.IsNot)) for op in n.ops):
            operands = [n.left, *n.comparators]
            for k, op in enumerate(n.ops):
                if isinstance(op, (ast.Is, ast.IsNot)):
                    lhs, rhs = operands[k], operands[k + 1]
                    if not any(isinstance(x, ast.Constant) and x.value is None for x in (lhs, rhs)):
                        bad(n, "identity comparison `is`/`is not` other than against the literal None (object "
                               "identity distinguishes callers, e.g. a fresh vs a shared int)")
        elif isinstance(n, ast.Name) and n.id in PURE_FORBIDDEN_NAMES:
            bad(n, f"{n.id} is not allowed in pure modules (hash-seed or identity dependent)")
        elif isinstance(n, ast.Name):
            if n.id.startswith("__"):
                bad(n, f"dunder name {n.id}")
            elif isinstance(n.ctx, ast.Load) and n.id not in bound and n.id not in PURE_BUILTINS:
                bad(n, f"name {n.id!r} is neither defined in the module nor an allowed pure builtin")
        elif isinstance(n, ast.Attribute):
            if n.attr.startswith("_") or n.attr in PURE_FORBIDDEN_ATTRS or \
                    n.attr.startswith(PURE_FORBIDDEN_ATTR_PREFIXES):
                bad(n, f"attribute .{n.attr}")
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                base = n.value
                while isinstance(base, (ast.Attribute, ast.Subscript)):
                    base = base.value
                if isinstance(base, ast.Name) and (base.id in module_level or base.id in PURE_BUILTINS):
                    bad(n, f"attribute assignment on module-level object {base.id} (state between calls)")
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)):
            base = n.value
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Name) and base.id in module_level:
                bad(n, f"item assignment on module-level object {base.id} (state between calls)")
    return out
