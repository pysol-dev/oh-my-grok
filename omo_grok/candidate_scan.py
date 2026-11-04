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
                          "gi_frame", "cr_frame", "ag_frame", "f_code", "co_code", "__reduce__", "__reduce_ex__"}
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
                imported_names.add(a.asname or a.name)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDER_ATTRS:
                bad(node, f"forbidden introspection attribute .{node.attr}")
            root = node.value.id if isinstance(node.value, ast.Name) else None
            mod = module_alias.get(root, root)
            if mod and (mod, node.attr) in FORBIDDEN_ATTRS and (mod, node.attr) not in allow_attrs:
                bad(node, f"forbidden reference {mod}.{node.attr}")
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
