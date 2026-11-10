"""UNTRUSTED unittest child. Spawned by verifier.py as `python -I -B -X pycache_prefix=<tmp> unittest_child.py
<start_dir>` with cwd = the candidate checkout and a scrubbed environment. It holds NO secret, nonce, result path or
result writer: candidate code runs in this process, so everything it reports is a self-report that adversarial code
can forge. The verifier treats the report as ADVISORY (a negative report fails the run; a positive one proves
nothing on its own). For oracle profiles the trusted decision comes from the separate oracle probe; for
non-oracle profiles the assurance level is 'cooperative'.

The report is one line on the original stdout: `OMG-UNITTEST-REPORT {json}`. The tamper snapshot (identities of
unittest machinery, builtins, key stdlib functions, import hooks; compared after import and after the run) is a cheap
tripwire for accidental or naive patching, not a security boundary."""
import builtins
import json
import operator
import os
import sys
import unittest
import unittest.case
import unittest.result
import unittest.suite

_dumps, _exit, _isinstance, _type = json.dumps, os._exit, isinstance, type
REPORT_PREFIX = "OMG-UNITTEST-REPORT "


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
_MODULES = ("builtins", "json", "operator", "os", "sys", "unittest", "unittest.case", "unittest.result",
            "unittest.suite", "unittest.loader", "unittest.runner", "__main__")
_FUNCS = {"os._exit": lambda: os._exit, "os.write": lambda: os.write, "os.open": lambda: os.open,
          "os.kill": lambda: os.kill,
          "sys.exit": lambda: sys.exit, "json.dumps": lambda: json.dumps, "json.dump": lambda: json.dump,
          "operator.eq": lambda: operator.eq,
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


def main():
    start_dir = sys.argv[1]
    top = os.getcwd()
    sys.path.insert(0, top)
    sys.dont_write_bytecode = True
    out = os.fdopen(os.dup(1), "w")  # report channel (untrusted anyway); candidate/test prints go to stderr
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def report(payload, code):
        out.write(REPORT_PREFIX + _dumps(payload, sort_keys=True) + "\n")
        out.flush()
        return code

    base = snapshot()
    rep = {"stage": "load", "tamper": [], "tests": {}, "counts": {}, "load_errors": []}
    try:
        suite = unittest.TestLoader().discover(os.path.join(top, start_dir), top_level_dir=top)
    except BaseException as e:  # noqa: BLE001 - any failure to load is a failed verification
        rep["load_errors"].append(f"{_type(e).__name__}: {e}"[:500])
        return report(rep, 1)
    rep["tamper"] = changed(base, snapshot())
    if rep["tamper"]:
        rep["stage"] = "after_import"
        return report(rep, 1)
    rep["stage"] = "run"
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Recorder)
    try:
        res = runner.run(suite)
    except BaseException as e:  # noqa: BLE001
        rep["load_errors"].append(f"run aborted: {_type(e).__name__}: {e}"[:500])
        return report(rep, 1)
    rep["tamper"] = changed(base, snapshot())
    if not _isinstance(res, Recorder) or _type(res) is not Recorder:
        rep["tamper"].append("result object is not the Recorder")
    else:
        rep["tests"] = dict(res.outcomes)
        rep["counts"] = {"run": res.testsRun, "started": res.started, "failures": len(res.failures),
                         "errors": len(res.errors), "skipped": len(res.skipped),
                         "expected_failures": len(res.expectedFailures),
                         "unexpected_successes": len(res.unexpectedSuccesses)}
        rep["load_errors"] = [t.id() for t, _ in res.errors if t.id().startswith("unittest.loader.")]
    rep["stage"] = "done"
    ok = not rep["tamper"] and rep["tests"] and all(v == "passed" for v in rep["tests"].values())
    return report(rep, 0 if ok else 1)


if __name__ == "__main__":
    code = main()
    for stream in (sys.__stdout__, sys.__stderr__):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    _exit(code)
