"""Real language servers (ruff pull, basedpyright push, typescript-language-server push) inside the bwrap sandbox.
Skipped when the bootstrap-installed tools or bwrap are absent, unless OMO_REQUIRE_TOOLS=1 (then they fail)."""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from omo_grok import sandbox
from omo_grok.lsp import profiles, runner
from omo_grok.paths import CODE_ROOT

CFG = profiles.load(CODE_ROOT / "config")
SBX = sandbox.probe()
FAKE = CODE_ROOT / "tests" / "unit" / "fake_lsp_server.py"


def _missing(names):
    out = [a for n in names for a in CFG["profiles"][n]["argv"] if a.startswith("/") and not os.path.isfile(a)]
    return out + ([] if SBX.ok else ["bwrap"])


def need(*names):
    miss = _missing(names)
    if miss and os.environ.get("OMO_REQUIRE_TOOLS") == "1":
        pytest.fail(f"OMO_REQUIRE_TOOLS=1 but missing: {miss}")
    return pytest.mark.skipif(bool(miss), reason=f"tools missing: {miss[:2]}")


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self._g("init", "-q")
        self._g("config", "user.email", "t@example.org")
        self._g("config", "user.name", "t")

    def _g(self, *a):
        return subprocess.run(["git", "-C", str(self.path), *a], check=True, capture_output=True, text=True).stdout

    def commit(self, files: dict, msg="c") -> str:
        for p, t in files.items():
            (self.path / p).parent.mkdir(parents=True, exist_ok=True)
            (self.path / p).write_text(t)
        self._g("add", "-A")
        self._g("commit", "-qm", msg)
        return self._g("rev-parse", "HEAD").strip()


def ev(repo, base, cand, tmp_path, only, cfg=CFG, **kw):
    return runner.evaluate(repo.path, base, cand, cfg, SBX, tmp_path / "scratch", only_profiles=only, **kw)


def codes(res):
    return sorted(x["code"] for x in res["new"])


PY_BASE = {"m.py": "def f(x: int) -> int:\n    return x + 1\n"}
PY_BAD = {"m.py": "def f(x: int) -> int:\n    y: int = 'no'\n    return x + undefined_q\n"}


@need("python-ruff")
def test_ruff_pull_canary_clean_and_new_finding(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    good = r.commit({"m.py": "def f(x: int) -> int:\n    return x + 2\n"})
    res = ev(r, base, good, tmp_path, ["python-ruff"])
    p = res["profiles"]["python-ruff"]
    assert res["status"] == "clean" and p["mode"] == "pull" and p["server"]["name"] == "ruff"
    assert p["canary"]["ok"] and "F821" in p["canary"]["bad_codes"]
    bad = r.commit(PY_BAD)
    res = ev(r, base, bad, tmp_path, ["python-ruff"])
    assert res["status"] == "findings" and "F821" in codes(res)


@need("python-basedpyright")
def test_basedpyright_push_catches_type_error_ruff_cannot_see(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    typed = r.commit({"m.py": "def f(x: int) -> int:\n    y: int = 'no'\n    return x + y\n"})
    res = ev(r, base, typed, tmp_path, ["python-basedpyright"])
    p = res["profiles"]["python-basedpyright"]
    assert res["status"] == "findings" and codes(res) == ["reportAssignmentType"]
    assert p["mode"] == "push" and p["server"]["name"] == "basedpyright" and p["canary"]["ok"]
    res = ev(r, base, typed, tmp_path, ["python-ruff"]) if not _missing(["python-ruff"]) else None
    if res:
        assert res["status"] == "clean", "the whole point: the linter alone misses a real type error"


@need("typescript")
def test_typescript_push_delta(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit({"a.ts": "export const a: number = 1;\n", "tsconfig.json": '{"compilerOptions": {"strict": true}}\n'})
    bad = r.commit({"a.ts": "export const a: number = 'x';\n", "tsconfig.json": '{"compilerOptions": {"strict": true}}\n'})
    res = ev(r, base, bad, tmp_path, ["typescript"])
    assert res["status"] == "findings" and codes(res) == ["2322"] and res["profiles"]["typescript"]["canary"]["ok"]
    good = r.commit({"a.ts": "export const a: number = 7;\n"})
    assert ev(r, base, good, tmp_path, ["typescript"])["status"] == "clean"


@need("python-basedpyright")
def test_preexisting_diagnostics_and_line_drift_are_not_new(tmp_path):
    r = Repo(tmp_path / "r")
    broken = "def f() -> int:\n    return 'x'\n"
    base = r.commit({"m.py": broken})
    drift = r.commit({"m.py": "# a\n# b\n# c\n\n" + broken, "ok.py": "VALUE: int = 3\n"})
    res = ev(r, base, drift, tmp_path, ["python-basedpyright"])
    assert res["status"] == "clean", res["new"]
    assert res["profiles"]["python-basedpyright"]["base_diagnostics"] == 1


@need("python-ruff")
def test_candidate_cannot_loosen_checker_config_or_suppress(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    cheat = r.commit({**PY_BAD, "pyproject.toml": '[tool.ruff.lint]\nignore = ["F821"]\n'})
    res = ev(r, base, cheat, tmp_path, ["python-ruff"])
    assert res["status"] == "findings"
    assert "F821" in codes(res), "base config is used, so the ignore has no effect"
    assert res["config_violations"] == ["pyproject.toml"]
    sup = r.commit({"m.py": "def f(x: int) -> int:\n    return x + undefined_q  # noqa: F821\n"})
    res = ev(r, base, sup, tmp_path, ["python-ruff"])
    assert res["status"] == "findings" and res["suppressions"] and not res["new"]


def test_checker_config_alone_is_a_finding_when_no_source_file_changed(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit({"tsconfig.json": '{"compilerOptions": {"strict": true}}\n'})
    cand = r.commit({"tsconfig.json": '{"compilerOptions": {"strict": false}}\n'})
    res = ev(r, base, cand, tmp_path, None)
    assert res["status"] == "findings" and res["config_violations"] == ["tsconfig.json"] and res["profiles"] == {}


@need("python-ruff")
def test_non_code_change_is_not_applicable_and_unknown_language_fails_closed(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    doc = r.commit({"README.md": "hi\n"})
    assert ev(r, base, doc, tmp_path, ["python-ruff"])["status"] == "not_applicable"
    go = r.commit({"main.go": "package main\n"})
    res = ev(r, doc, go, tmp_path, None)
    assert res["status"] == "unavailable" and res["uncovered"] == {"go": ["main.go"]}


@need("python-ruff")
def test_base_results_are_cached_per_tool_identity(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    cand = r.commit({"m.py": "def f(x: int) -> int:\n    return x\n"})
    cache = tmp_path / "cache"
    ev(r, base, cand, tmp_path, ["python-ruff"], cache_dir=cache)
    files = sorted(cache.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    data["diagnostics"] = [{"path": "m.py", "severity": "error", "code": "SENTINEL", "message": "x", "line": 0,
                            "character": 0, "source": None}]
    files[0].write_text(json.dumps(data))
    res = ev(r, base, cand, tmp_path, ["python-ruff"], cache_dir=cache)
    assert res["profiles"]["python-ruff"]["base_diagnostics"] == 1, "cache was used"
    data["identity"] = []
    files[0].write_text(json.dumps(data))
    res = ev(r, base, cand, tmp_path, ["python-ruff"], cache_dir=cache)
    assert res["profiles"]["python-ruff"]["base_diagnostics"] == 0, "changed tool identity invalidates the cache"


# ---------------------------------------------------------------- unavailable means fail closed, never clean
def stub_cfg(argv, tmp_path, mode="push", **over):
    cfg = copy.deepcopy(CFG)
    prof = cfg["profiles"]["python-ruff"]
    prof.update(argv=argv, tool_dirs=[str(FAKE.parent)], mode=mode, file_timeout_seconds=3, startup_timeout_seconds=5, **over)
    cfg["profiles"] = {"python-ruff": prof}
    return cfg


@pytest.mark.skipif(not SBX.ok, reason="bwrap unavailable")
@pytest.mark.parametrize("name,argv,reason", [
    ("missing binary", ["/nonexistent/ruff", "server"], "missing_tool"),
    ("exits 0 silently", ["/usr/bin/python3", "-c", "pass"], "lsp_crash"),
    ("initialize only, never diagnoses", ["/usr/bin/python3", str(FAKE), "silent"], "lsp_timeout"),
    ("crashes on first document", ["/usr/bin/python3", str(FAKE), "crash_on_open"], "lsp_crash"),
    ("garbage framing", ["/usr/bin/python3", str(FAKE), "malformed"], "lsp_protocol"),
])
def test_unusable_server_is_unavailable_not_clean(tmp_path, name, argv, reason):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    cand = r.commit({"m.py": "def f(x: int) -> int:\n    return x\n"})
    res = ev(r, base, cand, tmp_path, ["python-ruff"], cfg=stub_cfg(argv, tmp_path))
    assert res["status"] == "unavailable", name
    assert res["profiles"]["python-ruff"]["reason"] == reason, res["profiles"]["python-ruff"]
    assert res["new"] == []


@need("python-ruff")
def test_pull_server_that_returns_nothing_new_cannot_pass_the_canary(tmp_path):
    cfg = stub_cfg(["/usr/bin/python3", str(FAKE), "pull"], tmp_path, mode="pull")
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    cand = r.commit({"m.py": "def f(x: int) -> int:\n    return x\n"})
    res = ev(r, base, cand, tmp_path, ["python-ruff"], cfg=cfg)
    assert res["profiles"]["python-ruff"]["reason"] == "canary_failed"


def test_sandbox_unavailable_fails_closed(tmp_path):
    r = Repo(tmp_path / "r")
    base = r.commit(PY_BASE)
    cand = r.commit({"m.py": "def f(x: int) -> int:\n    return x\n"})
    no = sandbox.Probe(False, "none", "no sandbox available: test")
    res = runner.evaluate(r.path, base, cand, CFG, no, tmp_path / "s", only_profiles=["python-ruff"])
    assert res["status"] == "unavailable"
    assert res["profiles"]["python-ruff"]["reason"] == "sandbox_unavailable"
