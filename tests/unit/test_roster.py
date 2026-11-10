"""Pure roster rules: scope validation, floor table, class resolution, task sets, escalation, diff contradictions."""
import pytest

from oh_my_grok import roster
from oh_my_grok.errors import ValidationError

BASE = {"files_estimate": 1, "new_dependency": False, "external_api": False, "security_sensitive": False,
        "cross_cutting": False}


def S(**kw):
    return {**BASE, **kw}


@pytest.mark.parametrize("scope,cls", [
    (S(), "quick"),
    (S(files_estimate=3), "quick"),
    (S(files_estimate=4), "standard"),
    (S(cross_cutting=True), "standard"),
    (S(external_api=True), "standard"),
    (S(files_estimate=15), "standard"),
    (S(files_estimate=16), "heavy"),
    (S(new_dependency=True), "heavy"),
    (S(security_sensitive=True), "heavy"),
    (S(security_sensitive=True, external_api=True), "heavy"),
])
def test_floor_table(scope, cls):
    assert roster.floor(roster.validate_scope(scope))[0] == cls


def test_floor_reports_triggering_fields():
    assert roster.floor(S(security_sensitive=True, files_estimate=20)) == ("heavy",
                                                                        ["security_sensitive", "files_estimate>15"])
    assert roster.floor(S()) == ("quick", [])


@pytest.mark.parametrize("bad", [
    None, [], "x", {}, S(extra=1), {k: v for k, v in BASE.items() if k != "external_api"},
    S(files_estimate=0), S(files_estimate=10001), S(files_estimate=True), S(files_estimate=2.5),
    S(files_estimate="3"), S(new_dependency=1), S(external_api="yes"), S(cross_cutting=None),
])
def test_scope_validation_rejects_bad_attestations(bad):
    with pytest.raises(ValidationError):
        roster.validate_scope(bad)


def test_scope_validation_normalises_key_order():
    assert list(roster.validate_scope(dict(reversed(list(BASE.items()))))) == sorted(BASE)


def test_resolve_class_defaults_to_floor_and_allows_larger():
    sc = roster.validate_scope(S(external_api=True))
    assert roster.resolve_class(sc, None) == "standard"
    assert roster.resolve_class(sc, "standard") == "standard"
    assert roster.resolve_class(sc, "heavy") == "heavy"


def test_resolve_class_refuses_below_floor_and_names_the_floor():
    sc = roster.validate_scope(S(security_sensitive=True))
    with pytest.raises(ValidationError) as ei:
        roster.resolve_class(sc, "quick")
    d = ei.value.details
    assert d["floor"] == "heavy" and d["triggered_by"] == ["security_sensitive"]
    with pytest.raises(ValidationError):
        roster.resolve_class(sc, "none")
    with pytest.raises(ValidationError):
        roster.resolve_class(sc, "gigantic")


def test_task_sets():
    assert roster.task_kinds("quick", None) == ["plan"]
    assert roster.task_kinds("standard", S()) == ["explore", "analyze", "plan"]
    assert roster.task_kinds("standard", S(external_api=True)) == ["explore", "research", "analyze", "plan"]
    assert roster.task_kinds("heavy", S()) == ["explore", "research", "analyze", "plan"]
    assert roster.task_kinds("investigation", S()) == ["explore", "analyze"]
    assert roster.task_kinds("investigation", S(external_api=True)) == ["explore", "research", "analyze"]
    assert roster.task_kinds("investigation", None) == ["explore", "research", "analyze"]
    for k in roster.task_kinds("investigation", None):
        assert k not in roster.READ_ONLY_FORBIDDEN_KINDS


def test_escalation_only_goes_up():
    roster.check_escalation("quick", "standard")
    roster.check_escalation("quick", "heavy")
    roster.check_escalation("standard", "heavy")
    for cur, tgt in [("standard", "quick"), ("heavy", "heavy"), ("heavy", "standard"), ("quick", "quick"),
                     ("quick", "none"), ("none", "heavy"), (None, "heavy"), ("investigation", "heavy")]:
        with pytest.raises(ValidationError):
            roster.check_escalation(cur, tgt)
    assert roster.next_class("quick") == "standard" and roster.next_class("heavy") is None


def test_scope_contradictions():
    sc = roster.validate_scope(S(files_estimate=2))
    assert roster.scope_contradictions(sc, ["a.py", "b.py", "c.py", "d.py"]) == []
    assert roster.scope_contradictions(sc, list("abcde")) != []
    assert roster.scope_contradictions(sc, ["requirements.txt"])
    assert roster.scope_contradictions(sc, ["web/package.json"])
    assert roster.scope_contradictions(sc, [".github/workflows/ci.yml"])
    assert roster.scope_contradictions(sc, ["app/auth_utils.py"])
    assert roster.scope_contradictions(sc, ["src/Credentials.py"])
    attested = roster.validate_scope(S(files_estimate=2, new_dependency=True, security_sensitive=True))
    assert roster.scope_contradictions(attested, ["requirements.txt", "auth.py"]) == []
