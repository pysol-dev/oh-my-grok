import copy
import json

import pytest

from omo_grok.config import expand_placeholders
from omo_grok.errors import ValidationError
from omo_grok.lsp import delta, profiles
from omo_grok.paths import CODE_ROOT


def d(path="a.py", sev=1, code="X", msg="boom", line=3):
    return delta.normalize(path, {"severity": sev, "code": code, "message": msg,
                                  "range": {"start": {"line": line, "character": 0}}})


def test_identical_sets_have_no_delta():
    assert delta.new_diagnostics([d()], [d()]) == []


def test_line_drift_is_not_new():
    base = [d(line=3, msg="x not defined at 3:4")]
    cand = [d(line=40, msg="x not defined at 40:4")]
    assert delta.new_diagnostics(base, cand) == []


def test_extra_occurrence_is_new_multiset():
    new = delta.new_diagnostics([d()], [d(line=1), d(line=2)])
    assert len(new) == 1


def test_different_code_or_file_is_new():
    assert len(delta.new_diagnostics([d()], [d(), d(code="Y")])) == 1
    assert len(delta.new_diagnostics([d()], [d(), d(path="b.py")])) == 1


def test_fixed_diagnostics_are_not_penalised():
    assert delta.new_diagnostics([d(), d(code="Y")], [d()]) == []


def test_fail_on_filters_severity():
    warn = d(sev=2, code="W")
    assert delta.new_diagnostics([], [warn], ("error",)) == []
    assert len(delta.new_diagnostics([], [warn], ("error", "warning"))) == 1


def test_code_normalisation_dict_int_none():
    assert delta.norm_code({"value": 2322}) == "2322"
    assert delta.norm_code(2322) == "2322"
    assert delta.norm_code(None) == ""


def test_missing_severity_is_treated_as_error():
    assert delta.normalize("a.py", {"message": "m"})["severity"] == "error"


DIFF = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,3 @@
-x = 1  # noqa
+x = 1  # noqa
+y: int = "s"  # type: ignore
+z = 2  # noqa: E501
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-q  # noqa
"""


def test_suppressions_are_net_per_file():
    out = delta.suppressions(DIFF, [r"#\s*noqa\b", r"#\s*type:\s*ignore"])
    got = {(o["path"], o["pattern"]): o["net_added"] for o in out}
    assert got == {("m.py", r"#\s*type:\s*ignore"): 1, ("m.py", r"#\s*noqa\b"): 1}


def test_suppression_removal_is_not_flagged():
    assert delta.suppressions("--- a/m.py\n+++ b/m.py\n-x  # noqa\n", [r"#\s*noqa"]) == []


def test_config_violations_by_basename():
    assert delta.config_violations(["a/pyproject.toml", "src/x.py"], ["pyproject.toml"]) == ["a/pyproject.toml"]


def test_shipped_config_validates_and_scope_is_explicit():
    cfg = profiles.load(CODE_ROOT / "config")
    assert set(cfg["profiles"]) == {"python-ruff", "python-basedpyright", "typescript"}
    apply, uncovered = profiles.profiles_for(cfg, ["a.py", "b.ts", "c.go", "d.md", "e.js"])
    assert set(apply) == {"python-ruff", "python-basedpyright", "typescript"}
    assert set(uncovered) == {"go", "javascript"}


def test_code_extension_without_profile_is_reported():
    cfg = profiles.load(CODE_ROOT / "config")
    cfg["profiles"]["typescript"]["enabled"] = False
    apply, uncovered = profiles.profiles_for(cfg, ["b.ts"])
    assert not apply and uncovered == {"typescript": ["b.ts"]}


def _raw():
    return expand_placeholders(json.loads((CODE_ROOT / "config" / "lsp.json").read_text()))


def test_unmutated_expanded_config_is_valid():
    profiles.validate(_raw())


@pytest.mark.parametrize("mut", [
    lambda r: r["profiles"]["python-ruff"].update(argv=["ruff", "server"]),
    lambda r: r["profiles"]["python-ruff"].update(bogus=1),
    lambda r: r["profiles"]["python-ruff"].update(mode="sometimes"),
    lambda r: r["profiles"]["python-ruff"].update(fail_on=[]),
    lambda r: r["profiles"]["python-ruff"]["canary"]["bad"].update(expect_codes=[]),
    lambda r: r["profiles"]["python-ruff"]["canary"]["bad"].update(path="../x.py"),
    lambda r: r.update(version=2),
    lambda r: r.update(profiles={}),
])
def test_invalid_profiles_rejected(mut):
    raw = copy.deepcopy(_raw())
    mut(raw)
    with pytest.raises(ValidationError):
        profiles.validate(raw)
