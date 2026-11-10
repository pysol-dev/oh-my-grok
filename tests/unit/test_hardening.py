"""Regression tests for the 2026-11-04 live-run findings: hook config single source of truth, stale MCP server."""
import json

import pytest

from oh_my_grok.hooks import set_hook
from oh_my_grok.paths import get_paths


def test_set_hook_directly_is_enough_for_dispatch(omg_home):
    from oh_my_grok.engine import Engine
    e = Engine()
    try:
        assert e._hook_ok("explorer") is False
        set_hook(get_paths(), "explorer", "https://hooks.invalid/explorer", "k" * 32)  # no mark_hook_configured
        assert e._hook_ok("explorer") is True
        listed = {b["role"]: b["webhook_configured"] for b in e.list_bindings()}
        assert all(listed[r] == e._hook_ok(r) for r in listed)
    finally:
        e.close()


def test_hooks_meta_flag_alone_does_not_enable_dispatch(omg_home):
    from oh_my_grok.engine import Engine
    e = Engine()
    try:
        e.mark_hook_configured("librarian", True)  # audit trail only, no secret on disk
        assert e._hook_ok("librarian") is False
    finally:
        e.close()


def test_mcp_server_refuses_when_code_or_config_changed(omg_home, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    from oh_my_grok import mcp_server
    monkeypatch.setattr(mcp_server, "STARTUP_FINGERPRINT", mcp_server.fingerprint())
    assert mcp_server._call(lambda e: {"ok": True}) == {"ok": True}
    monkeypatch.setattr(mcp_server, "fingerprint", lambda: "0" * 64)
    with pytest.raises(ToolError) as ei:
        mcp_server._call(lambda e: {"ok": True})
    assert json.loads(str(ei.value))["error"] == "stale_server"


def test_fingerprint_changes_with_config(omg_home, tmp_path, monkeypatch):
    from oh_my_grok import fingerprint as fp
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "verifiers.json").write_text("{}")
    monkeypatch.setenv("OMG_CONFIG_DIR", str(cfg))
    a = fp.fingerprint()
    (cfg / "verifiers.json").write_text('{"x": 1}')
    assert fp.fingerprint() != a
