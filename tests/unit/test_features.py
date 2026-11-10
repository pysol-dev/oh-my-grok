"""Feature modes: conservative defaults, strict validation, per-job freeze (spec/ADAPTIVE.md section 10, R14)."""
import json
import os
from pathlib import Path

import pytest

from oh_my_grok import features
from oh_my_grok.errors import ValidationError


def test_all_features_default_off():
    d = features.defaults()
    assert set(d) == {"adaptive_roster", "boulder", "lsp", "git_discipline", "docs_evidence", "repo_lease"}
    assert set(d.values()) == {"off"}


def test_partial_mapping_is_completed_with_off():
    out = features.normalize({"lsp": "shadow"})
    assert out["lsp"] == "shadow" and out["adaptive_roster"] == "off"


@pytest.mark.parametrize("raw", [{"nope": "off"}, {"lsp": "maybe"}, {"boulder": "shadow"}, {"boulder": "enforce"},
                                 {"lsp": True}, ["lsp"], "lsp"])
def test_unknown_names_and_modes_are_refused(raw):
    with pytest.raises(ValidationError):
        features.normalize(raw)


def test_job_without_frozen_features_runs_with_everything_off():
    assert features.frozen({}) == features.defaults()
    assert features.frozen(None) == features.defaults()
    assert features.frozen({"features": {"lsp": "enforce"}})["lsp"] == "enforce"


def _write_policy(features_obj):
    cfg = Path(os.environ["OMG_CONFIG_DIR"])
    pol = json.loads((cfg / "policy.json").read_text())
    pol["features"] = features_obj
    (cfg / "policy.json").write_text(json.dumps(pol))


def test_policy_json_features_are_validated_on_load(omg_home):
    from oh_my_grok.engine import Engine
    _write_policy({"lsp": "sideways"})
    with pytest.raises(ValidationError):
        Engine()


def test_policy_features_are_frozen_into_the_job(omg_home):
    from oh_my_grok.engine import Engine
    _write_policy({"adaptive_roster": "enforce"})
    e = Engine()
    job = e.submit("goal", "freeze-key-0001", mode="plan_only", intent="implementation",
                   scope={"files_estimate": 1, "new_dependency": False, "external_api": False,
                          "security_sensitive": False, "cross_cutting": False})["job_id"]
    e.policy["features"] = features.defaults()  # later configuration change
    row = e._job(job)
    assert e._features(row)["adaptive_roster"] == "enforce"
    assert e._policy(row)["features"]["adaptive_roster"] == "enforce"
    e.close()


def test_legacy_submit_freezes_the_current_effective_set(omg_home):
    from oh_my_grok.engine import Engine
    e = Engine()
    job = e.submit("goal", "freeze-key-0002", mode="plan_only")["job_id"]
    assert e._features(e._job(job)) == features.defaults()
    e.close()
