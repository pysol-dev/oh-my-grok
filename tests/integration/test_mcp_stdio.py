"""Real MCP SDK stdio client against the real server process (success criterion + §10 item 10)."""
import asyncio
import json
import os
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python")
SERVER = str(ROOT / "omo_grok" / "mcp_server.py")
EXPECTED = {"submit", "claim", "context", "checkpoint", "complete", "status", "reconcile", "cancel"}


def _params():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "OMO_HOME", "OMO_CONFIG_DIR")}
    return StdioServerParameters(command=PY, args=[SERVER], env=env)


def _payload(res):
    assert res.content and not res.is_error, res
    return res.structured_content or json.loads(res.content[0].text)


async def _session_flow(errlog):
    async with stdio_client(_params(), errlog=errlog) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert init.server_info.name == "omo"
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            sub = await s.call_tool("submit", {"goal": "Fix calc.add in the synthetic fixture",
                                               "idempotency_key": "mcp-stdio-0001", "mode": "fixture",
                                               "acceptance_criteria": ["unittest exits 0"]})
            assert not sub.is_error, sub
            job = _payload(sub)["job_id"]
            again = _payload(await s.call_tool("submit", {"goal": "Fix calc.add in the synthetic fixture",
                                                          "idempotency_key": "mcp-stdio-0001", "mode": "fixture",
                                                          "acceptance_criteria": ["unittest exits 0"]}))
            reuse = await s.call_tool("submit", {"goal": "something else", "idempotency_key": "mcp-stdio-0001"})
            assert reuse.is_error and '"conflict"' in reuse.content[0].text
            st = _payload(await s.call_tool("status", {"job_id": job}))
            bad = await s.call_tool("approve", {"job_id": job, "plan_hash": "x" * 64})
            inval = await s.call_tool("claim", {"job_id": "not-a-uuid", "task_id": job, "dispatch_id": job})
            missing = await s.call_tool("status", {"job_id": "00000000-0000-4000-8000-000000000000"})
            return names, tools, job, again, st, bad, inval, missing


def test_sdk_stdio_initialize_list_submit_status(omo_home, tmp_path):
    log = tmp_path / "stderr.log"
    with open(log, "w") as errlog:
        names, tools, job, again, st, bad, inval, missing = asyncio.run(_session_flow(errlog))
    assert names == EXPECTED
    assert not names & {"approve", "deny", "ship", "mark_done", "shell", "exec", "secret", "set_hook", "bind_role"}
    for t in tools.tools:
        assert t.input_schema.get("type") == "object"
    assert again["job_id"] == job and again["created"] is False
    assert st["job"]["id"] == job if "job" in st else st["job_id"] == job
    assert bad.is_error  # item 10: no approval tool exists over MCP
    assert inval.is_error
    assert missing.is_error and "not_found" in missing.content[0].text
    # stdout carried only protocol (the SDK would have failed otherwise); logs went to stderr
    assert "sk-" not in log.read_text()


def test_item10_server_stdout_is_protocol_only(omo_home):
    """Drive the raw server with one JSON-RPC message; every stdout line must parse as JSON-RPC."""
    import subprocess
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}
    p = subprocess.run([PY, SERVER], input=json.dumps(init) + "\n", capture_output=True, text=True, timeout=30,
                       env=os.environ.copy())
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    assert lines
    for ln in lines:
        assert json.loads(ln)["jsonrpc"] == "2.0"
