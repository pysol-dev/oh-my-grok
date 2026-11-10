import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oh_my_grok import context7
from oh_my_grok.context7 import Context7Error

SERVER = str(Path(__file__).parent / "fake_context7.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(tmp_path):
    procs = []

    def start(mode="ok"):
        port = free_port()
        log = tmp_path / f"spy-{mode}.log"
        p = subprocess.Popen([sys.executable, SERVER, str(port)], env={**os.environ, "FAKE_C7_MODE": mode,
                                                                       "FAKE_C7_LOG": str(log)},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("fake context7 did not start")
        return f"http://127.0.0.1:{port}/mcp", log

    yield start
    for p in procs:
        p.kill()
        p.wait()


def test_fetch_resolves_library_then_queries_docs(server):
    url, _ = server()
    r = context7.fetch(url, "how do retries work", library_name="widget")
    assert r.library_id == "/acme/widget" and r.library_title == "Widget"
    assert r.server == {"name": "FakeContext7", "version": "9.9.9"} and r.protocol_version
    assert [c["tool"] for c in r.calls] == ["resolve-library-id", "query-docs"]
    assert "Widget.configure(retries=3)" in r.text and r.calls[1]["response"] == r.text
    assert r.calls[1]["arguments"] == {"libraryId": "/acme/widget", "query": "how do retries work"}


def test_known_library_id_skips_resolution(server):
    url, _ = server()
    r = context7.fetch(url, "q", library_id="/acme/widget")
    assert [c["tool"] for c in r.calls] == ["query-docs"]


@pytest.mark.parametrize("mode,kind", [("no_library", "no_library"), ("error_resolve", "tool_error"),
                                       ("error_query", "tool_error"), ("empty", "tool_error")])
def test_failures_raise_and_never_return_text(server, mode, kind):
    url, _ = server(mode)
    with pytest.raises(Context7Error) as e:
        context7.fetch(url, "q", library_name="widget")
    assert e.value.kind == kind


def test_unreachable_endpoint_is_connect_error():
    with pytest.raises(Context7Error) as e:
        context7.fetch(f"http://127.0.0.1:{free_port()}/mcp", "q", library_name="widget", timeout=3)
    assert e.value.kind == "connect"


def test_api_key_is_sent_as_bearer_and_never_in_errors(server):
    url, log = server()
    context7.fetch(url, "q", library_name="widget", api_key="ctx7sk-SECRET-123")
    auths = {json.loads(l)["auth"] for l in log.read_text().splitlines()}
    assert auths == {"Bearer ctx7sk-SECRET-123"}
    with pytest.raises(Context7Error) as e:
        context7.fetch(f"http://127.0.0.1:{free_port()}/mcp", "q", library_name="w", api_key="ctx7sk-SECRET-123",
                       timeout=3)
    assert "ctx7sk-SECRET-123" not in str(e.value)


@pytest.mark.parametrize("kw", [{"query": ""}, {"query": "q"}, {"query": "q", "library_id": "not-an-id"}])
def test_argument_validation(kw):
    with pytest.raises(Context7Error):
        context7.fetch("http://127.0.0.1:1/mcp", **kw)
