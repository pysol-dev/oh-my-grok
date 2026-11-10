import sys
import time
from pathlib import Path

import pytest

from oh_my_grok import sandbox
from oh_my_grok.lsp import profiles, runner
from oh_my_grok.lsp.client import LspClient, LspError, MethodNotFound

FAKE = str(Path(__file__).parent / "fake_lsp_server.py")


def mk(scenario, **kw):
    c = LspClient([sys.executable, FAKE, scenario], **kw)
    c.start()
    c.request("initialize", {"capabilities": {}}, timeout=10)
    c.notify("initialized", {})
    return c


def open_doc(c, uri="file:///w/a.py", version=1):
    c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": version,
                                                       "text": "x"}})
    return uri


def test_push_filters_stale_versions_and_returns_fresh():
    c = mk("push_versioned")
    try:
        uri = open_doc(c, version=3)
        d = c.wait_push(uri, 3, quiet=0.3, deadline=10)
        assert [x["message"] for x in d] == ["fresh"]
    finally:
        c.close()


def test_push_without_version_waits_for_quiet_and_takes_last():
    c = mk("push_unversioned")
    try:
        uri = open_doc(c)
        d = c.wait_push(uri, 1, quiet=0.5, deadline=10)
        assert [x["message"] for x in d] == ["late"]
    finally:
        c.close()


def test_push_deadline_is_failure_not_empty():
    c = mk("silent")
    try:
        uri = open_doc(c)
        with pytest.raises(LspError) as e:
            c.wait_push(uri, 1, quiet=0.1, deadline=0.6)
        assert e.value.kind == "timeout"
    finally:
        c.close()


def test_push_continuous_chatter_hits_deadline():
    c = mk("flood")
    try:
        uri = open_doc(c)
        with pytest.raises(LspError) as e:
            c.wait_push(uri, 1, quiet=0.5, deadline=1.0)
        assert e.value.kind == "timeout"
    finally:
        c.close()


def test_pull_request_returns_items():
    c = mk("pull")
    try:
        uri = open_doc(c)
        r = c.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, timeout=10)
        assert r["items"][0]["code"] == "P1"
    finally:
        c.close()


def test_pull_server_error_is_lsp_error():
    c = mk("pull_error")
    try:
        with pytest.raises(LspError) as e:
            c.request("textDocument/diagnostic", {"textDocument": {"uri": "file:///w/a.py"}}, timeout=10)
        assert e.value.kind == "server_error"
    finally:
        c.close()


def test_request_timeout():
    c = mk("pull_hang")
    try:
        with pytest.raises(LspError) as e:
            c.request("textDocument/diagnostic", {"textDocument": {"uri": "file:///w/a.py"}}, timeout=0.5)
        assert e.value.kind == "timeout"
    finally:
        c.close()


def test_server_crash_surfaces_as_crash_not_empty():
    c = mk("crash_on_open")
    try:
        uri = open_doc(c)
        with pytest.raises(LspError) as e:
            c.wait_push(uri, 1, quiet=0.1, deadline=10)
        assert e.value.kind == "crash"
        with pytest.raises(LspError):
            c.request("anything", None, timeout=1)
    finally:
        c.close()


@pytest.mark.parametrize("scenario,kind", [("malformed", "protocol"), ("badjson", "protocol"),
                                           ("oversize", "oversize")])
def test_bad_frames_fail_closed(scenario, kind):
    c = mk(scenario, max_message=1 << 20)
    try:
        uri = open_doc(c)
        with pytest.raises(LspError) as e:
            c.wait_push(uri, 1, quiet=0.1, deadline=10)
        assert e.value.kind == kind
    finally:
        c.close()


def test_workspace_configuration_is_answered_by_handler():
    def handler(method, params):
        assert method == "workspace/configuration"
        return [{"k": 1}, None]
    c = mk("config_request", request_handler=handler)
    try:
        uri = open_doc(c)
        d = c.wait_push(uri, 1, quiet=0.3, deadline=10)
        assert '"result": [{"k": 1}, null]' in d[0]["message"]
    finally:
        c.close()


def test_unknown_server_request_gets_method_not_found():
    def handler(method, params):
        raise MethodNotFound(method)
    c = mk("unknown_request", request_handler=handler)
    try:
        uri = open_doc(c)
        d = c.wait_push(uri, 1, quiet=0.3, deadline=10)
        assert "-32601" in d[0]["message"]
        assert "foo/bar" in c.server_requests
    finally:
        c.close()


def test_missing_binary_is_lsp_error():
    c = LspClient(["/nonexistent/server"])
    with pytest.raises(LspError) as e:
        c.start()
    assert e.value.kind == "crash"


def test_close_kills_unresponsive_server_promptly():
    c = mk("pull_hang")
    t = time.monotonic()
    c.close(graceful_timeout=0.5)
    assert time.monotonic() - t < 5
    assert c.proc.poll() is not None


def test_pull_profile_against_a_push_server_does_not_leave_the_child_running(tmp_path):
    sbx = sandbox.probe()
    if not sbx.ok:
        pytest.skip(sbx.detail)
    work = tmp_path / "work"
    work.mkdir()
    fake = Path(__file__).resolve().parent / "fake_lsp_server.py"
    prof = {**profiles.PROFILE_DEFAULTS, "mode": "pull", "language": "python", "extensions": [".py"],
            "argv": ["/usr/bin/python3", str(fake), "push_versioned"], "init_options": {}, "settings": {},
            "tool_dirs": [str(fake.parent)], "address_space_mb": None, "startup_timeout_seconds": 10,
            "canary": {"bad": {"path": "b.py", "text": "x\n", "expect_codes": ["E1"]},
                       "good": {"path": "g.py", "text": "y\n"}}}
    sess = runner.Session(sbx, prof, work, time.monotonic() + 30)
    with pytest.raises(runner.Unavailable):
        with sess:
            pass
    assert sess.client.proc is not None and sess.client.proc.poll() is not None
