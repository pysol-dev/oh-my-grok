"""Trusted verifier runner. Lives in the orchestrator tree (never in the candidate checkout) and is started by
verifier.py as `python -I -B -X pycache_prefix=<private> trusted_runner.py` with cwd = the candidate checkout.

Protocol (all channels are verifier-owned and outside the candidate tree):
  OMO_VERIFY_NONCE_FD  inherited pipe fd carrying a per-run random nonce; read and closed BEFORE any candidate import
  OMO_VERIFY_SPEC      JSON spec: start_dir, top_level_dir, expected_tests, type_checks
  OMO_VERIFY_RESULT    path of the JSON result file to write (atomic)
The verifier passes a candidate only if exit == 0 AND the result file exists AND carries the nonce AND every expected
test id passed AND the counts match AND no tamper was detected. A candidate that calls os._exit(0) therefore yields
no result file and fails.

Tamper detection: identities of unittest assert*/run machinery, this runner's own result class, every builtin, key
stdlib functions, sys.modules entries for the runner's dependencies, sys.meta_path/path_hooks and trace/profile hooks
are snapshotted before the candidate is imported and compared after import and after the run.

This raises the bar; it cannot make adversarial in-process code safe in general (see docs/SECURITY.md)."""
import builtins
import io
import json
import operator
import os
import sys
import unittest
import unittest.case
import unittest.result
import unittest.suite

_open, _dumps, _replace, _fsync, _exit, _isinstance, _type = (io.open, json.dumps, os.replace, os.fsync, os._exit,
                                                               isinstance, type)
_TYPES = {"int": int, "float": float, "str": str, "bool": bool, "list": list, "tuple": tuple, "dict": dict,
          "bytes": bytes, "NoneType": type(None)}


class Recorder(unittest.TextTestResult):
    """Records a per-test outcome keyed by test id. Only an explicit addSuccess counts as passed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.outcomes = {}
        self.started = 0

    def startTest(self, test):
        self.started += 1
        super().startTest(test)

    def _set(self, test, outcome):
        tid = test.id()
        # a test reported twice (e.g. success then error in teardown) keeps the worst outcome
        if self.outcomes.get(tid, "passed") == "passed":
            self.outcomes[tid] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._set(test, "failed")


_CLASSES = (unittest.TestCase, unittest.TestResult, unittest.TextTestResult, unittest.TextTestRunner,
            unittest.TestSuite, unittest.suite.BaseTestSuite, unittest.TestLoader, Recorder)
_MODULES = ("builtins", "io", "json", "operator", "os", "sys", "unittest", "unittest.case", "unittest.result",
            "unittest.suite", "unittest.loader", "unittest.runner", "__main__")
_FUNCS = {"os._exit": lambda: os._exit, "os.write": lambda: os.write, "os.open": lambda: os.open,
          "os.replace": lambda: os.replace, "os.fsync": lambda: os.fsync, "os.kill": lambda: os.kill,
          "sys.exit": lambda: sys.exit, "json.dumps": lambda: json.dumps, "json.dump": lambda: json.dump,
          "io.open": lambda: io.open, "operator.eq": lambda: operator.eq,
          "unittest.case.safe_repr": lambda: unittest.case.safe_repr,
          "unittest.main": lambda: unittest.main}


def snapshot():
    snap = {}
    for cls in _CLASSES:
        for k in cls.__mro__:
            if k is object:
                continue
            for name, val in vars(k).items():
                snap[f"{cls.__qualname__}:{k.__qualname__}.{name}"] = val
            snap[f"{cls.__qualname__}:{k.__qualname__}.__keys__"] = tuple(sorted(vars(k)))
    for name, val in vars(builtins).items():
        snap[f"builtins.{name}"] = val
    snap["builtins.__keys__"] = tuple(sorted(vars(builtins)))
    for name, get in _FUNCS.items():
        snap[name] = get()
    for m in _MODULES:
        snap[f"sys.modules[{m}]"] = sys.modules.get(m)
    snap["sys.meta_path"] = tuple(sys.meta_path)
    snap["sys.path_hooks"] = tuple(sys.path_hooks)
    snap["sys.gettrace"] = sys.gettrace()
    snap["sys.getprofile"] = sys.getprofile()
    snap["sys.excepthook"] = sys.excepthook
    snap["sys.displayhook"] = sys.displayhook
    snap["Recorder"] = sys.modules["__main__"].__dict__.get("Recorder")
    return snap


def changed(before, after):
    keys = set(before) | set(after)
    def differs(k):
        if k not in before or k not in after:
            return True
        if k.endswith("__keys__"):
            return before[k] != after[k]  # tuples of str built fresh each snapshot: compare by value
        a, b = before[k], after[k]
        if k in ("sys.meta_path", "sys.path_hooks"):  # fresh tuples: compare element identities
            return len(a) != len(b) or any(x is not y for x, y in zip(a, b))
        return a is not b
    return sorted(k for k in keys if differs(k))


def write_result(path, nonce, payload):
    payload = dict(payload, nonce=nonce)
    tmp = path + ".tmp"
    with _open(tmp, "w") as fh:
        fh.write(_dumps(payload, sort_keys=True))
        fh.flush()
        _fsync(fh.fileno())
    _replace(tmp, path)


def main():
    fd = int(os.environ.pop("OMO_VERIFY_NONCE_FD"))
    result_path = os.environ.pop("OMO_VERIFY_RESULT")
    spec_path = os.environ.pop("OMO_VERIFY_SPEC")
    with os.fdopen(fd, "r") as fh:
        nonce = fh.read().strip()
    with _open(spec_path) as fh:
        spec = json.load(fh)
    top = os.path.abspath(spec.get("top_level_dir", "."))
    sys.path.insert(0, top)
    sys.dont_write_bytecode = True
    base = snapshot()
    out = {"stage": "load", "tamper": [], "tests": {}, "counts": {}, "type_checks": [], "load_errors": []}
    try:
        suite = unittest.TestLoader().discover(os.path.join(top, spec["start_dir"]), top_level_dir=top)
    except BaseException as e:  # noqa: BLE001 - any failure to load is a failed verification
        out["load_errors"].append(f"{_type(e).__name__}: {e}"[:500])
        write_result(result_path, nonce, out)
        return 1
    out["tamper"] = changed(base, snapshot())
    if out["tamper"]:
        out["stage"] = "after_import"
        write_result(result_path, nonce, out)
        return 1
    out["stage"] = "run"
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Recorder)
    try:
        res = runner.run(suite)
    except BaseException as e:  # noqa: BLE001
        out["load_errors"].append(f"run aborted: {_type(e).__name__}: {e}"[:500])
        write_result(result_path, nonce, out)
        return 1
    out["tamper"] = changed(base, snapshot())
    if not _isinstance(res, Recorder) or _type(res) is not Recorder:
        out["tamper"].append("result object is not the trusted Recorder")
    else:
        out["tests"] = dict(res.outcomes)
        out["counts"] = {"run": res.testsRun, "started": res.started, "failures": len(res.failures),
                         "errors": len(res.errors), "skipped": len(res.skipped),
                         "expected_failures": len(res.expectedFailures),
                         "unexpected_successes": len(res.unexpectedSuccesses)}
        out["load_errors"] = [t.id() for t, _ in res.errors if t.id().startswith("unittest.loader.")]
    for chk in spec.get("type_checks", []):
        rec = {"call": chk["call"], "ok": False}
        try:
            modname, fn = chk["call"].rsplit(".", 1)
            r = getattr(sys.modules.get(modname) or __import__(modname, fromlist=[fn]), fn)(*chk.get("args", []))
            want = _TYPES[chk["type"]]
            rec["type"] = _type(r).__name__
            rec["ok"] = _type(r) is want and ("expect" not in chk or operator.eq(r, chk["expect"]))
            rec["ok"] = rec["ok"] is True
        except BaseException as e:  # noqa: BLE001
            rec["error"] = f"{_type(e).__name__}: {e}"[:300]
        out["type_checks"].append(rec)
    out["tamper"] += [t for t in changed(base, snapshot()) if t not in out["tamper"]]
    out["stage"] = "done"
    write_result(result_path, nonce, out)
    ok = (not out["tamper"] and out["tests"] and all(v == "passed" for v in out["tests"].values())
          and all(c["ok"] for c in out["type_checks"]))
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    for stream in (sys.__stdout__, sys.__stderr__):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    _exit(code)
