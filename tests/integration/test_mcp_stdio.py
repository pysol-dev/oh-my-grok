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
EXPECTED = {"submit", "claim", "context", "checkpoint", "complete", "status", "reconcile", "cancel", "escalate",
            "docs"}


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
    assert st["job_id"] == job and st["mode"] == "fixture" and st["phase"] == "research"
    assert bad.is_error  # item 10: no approval tool exists over MCP
    assert inval.is_error
    assert missing.is_error and "not_found" in missing.content[0].text
    # stdout carried only protocol (the SDK would have failed otherwise); the secret-leak check with real planted
    # secrets lives in test_planted_secrets_never_leak_via_mcp_status_log_or_export below


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


# ---------------------------------------------------------------------------------------------------- secret leaks
HOOK_KEY = "whsec_PLANTED_hookkey_0123456789abcdef"
ENV_SECRET = "sk-PLANTEDenvSECRET0123456789abcdef"
URL_TOKEN = "PLANTEDurlTOKEN0123456789"
ERR_SECRET = "zq7KpMyeHeRdIM1LWpmAKNupNNgJt8ize6nWGPA2HabaY"  # pattern-free: no redaction regex can match it


async def _leak_flow(errlog, env):
    params = StdioServerParameters(command=PY, args=[SERVER], env=env)
    async with stdio_client(params, errlog=errlog) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            outs = []
            sub = await s.call_tool("submit", {"goal": "Fix calc.add in the synthetic fixture",
                                               "idempotency_key": "leak-0001", "mode": "fixture",
                                               "acceptance_criteria": ["unittest exits 0"]})
            job = _payload(sub)["job_id"]
            outs.append(sub)
            outs.append(await s.call_tool("status", {"job_id": job}))
            outs.append(await s.call_tool("status", {}))
            outs.append(await s.call_tool("reconcile", {"job_id": job}))
            # error paths carrying a secret in the input
            outs.append(await s.call_tool("status", {"job_id": ERR_SECRET}))
            outs.append(await s.call_tool("claim", {"job_id": job, "task_id": job, "dispatch_id": ERR_SECRET}))
            outs.append(await s.call_tool("context", {"job_id": job, "task_id": ERR_SECRET}))
            # exact, field-name-only error shape (no input value, no pattern-dependent redaction)
            for o, (tool, field) in zip(outs[-3:], [("status", "job_id"), ("claim", "dispatch_id"),
                                                    ("context", "task_id")]):
                assert o.is_error and len(o.content) == 1
                assert o.content[0].text == json.dumps({"error": "invalid", "fields": [field],
                                                        "message": f"invalid arguments for {tool}"}, sort_keys=True)
            bad = await s.call_tool("submit", {"goal": "x", "idempotency_key": "leak-0002", "mode": "fixture",
                                               "verifier_profile": ERR_SECRET})
            assert bad.is_error and "unknown verifier_profile" in bad.content[0].text
            outs.append(bad)
            text = "\n".join(json.dumps(o.structured_content) + "\n" + "\n".join(
                getattr(c, "text", "") for c in o.content) for o in outs)
            return job, text


def test_planted_secrets_never_leak_via_mcp_status_log_or_export(omo_home, tmp_path, monkeypatch):
    """Real secrets planted in the hooks file (key + URL path token), the server's environment and tool-call inputs
    must never appear in MCP results, the server's stderr log, `omoctl status/export`, or the export file."""
    import subprocess
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from helpers import Harness
    from omo_grok.hooks import set_hook
    from omo_grok.paths import get_paths
    monkeypatch.setenv("OMO_ALLOW_INSECURE_HOOKS", "1")
    h = Harness()  # binds every role (verified) and writes hooks; then plant a real secret-bearing hook
    for role in ("explorer", "librarian"):  # closed loopback port: connection refused, error may echo the URL
        set_hook(get_paths(), role, f"http://127.0.0.1:9/routines/{URL_TOKEN}/fire?sig={URL_TOKEN}", HOOK_KEY)
    h.close()
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "OMO_HOME", "OMO_CONFIG_DIR",
                                                        "OMO_ALLOW_INSECURE_HOOKS")}
    env["OPENAI_API_KEY"] = ENV_SECRET
    env["OMO_DEBUG_SECRET"] = ENV_SECRET
    log = tmp_path / "stderr.log"
    with open(log, "w") as errlog:
        job, mcp_text = asyncio.run(_leak_flow(errlog, env))
    # the dispatch really happened and really failed (otherwise this test proves nothing)
    from omo_grok.engine import Engine
    e = Engine()
    try:
        rows = e._all("SELECT status, last_error FROM dispatch_outbox WHERE job_id=?", (job,))
    finally:
        e.close()
    assert rows and all(r["status"] in ("retry_wait", "failed") and r["last_error"] for r in rows), rows
    omoctl = str(ROOT / "omoctl")
    st = subprocess.run([omoctl, "status", job, "--json"], capture_output=True, text=True, env=env, timeout=60)
    allst = subprocess.run([omoctl, "status", "--json"], capture_output=True, text=True, env=env, timeout=60)
    hk = subprocess.run([omoctl, "hook", "list"], capture_output=True, text=True, env=env, timeout=60)
    ex = subprocess.run([omoctl, "export", job, "--out", str(tmp_path / "exp")], capture_output=True, text=True,
                        env=env, timeout=60)
    assert st.returncode == 0 and allst.returncode == 0 and hk.returncode == 0 and ex.returncode == 0, \
        (st.stderr, allst.stderr, hk.stderr, ex.stderr)
    export_text = "".join(p.read_text() for p in (tmp_path / "exp").glob("*.json"))
    assert export_text
    surfaces = {"mcp": mcp_text, "stderr_log": log.read_text(), "status": st.stdout + st.stderr,
                "status_all": allst.stdout + allst.stderr, "hook_list": hk.stdout + hk.stderr,
                "export_cli": ex.stdout + ex.stderr, "export_file": export_text}
    for name, text in surfaces.items():
        for secret in (HOOK_KEY, ENV_SECRET, URL_TOKEN, ERR_SECRET):
            assert secret not in text, f"{secret[:12]}... leaked via {name}"
    assert '"last_error"' in export_text and job in export_text  # export really carries the failed dispatches


SCOPE = {"files_estimate": 2, "new_dependency": False, "external_api": False, "security_sensitive": False,
         "cross_cutting": False}


async def _adaptive_flow(errlog):
    async with stdio_client(_params(), errlog=errlog) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            expl = _payload(await s.call_tool("submit", {"goal": "What does calc.add do?",
                                                         "idempotency_key": "mcp-adaptive-0001",
                                                         "intent": "explanation"}))
            inv = _payload(await s.call_tool("submit", {"goal": "Why is add wrong?",
                                                        "idempotency_key": "mcp-adaptive-0002",
                                                        "intent": "investigation"}))
            bad_scope = await s.call_tool("submit", {"goal": "x", "idempotency_key": "mcp-adaptive-0003",
                                                     "intent": "implementation",
                                                     "scope": {**SCOPE, "surprise": True}})
            low = await s.call_tool("submit", {"goal": "x", "idempotency_key": "mcp-adaptive-0004",
                                               "intent": "implementation", "mode": "fixture", "roster_class": "quick",
                                               "scope": {**SCOPE, "security_sensitive": True}})
            docs_off = await s.call_tool("docs", {"job_id": inv["job_id"], "query": "q", "library_name": "pytest"})
            down = await s.call_tool("escalate", {"job_id": inv["job_id"], "to_class": "heavy", "reason": "r"})
            down_bad = await s.call_tool("escalate", {"job_id": inv["job_id"], "to_class": "quick", "reason": "r"})
            return expl, inv, bad_scope, low, docs_off, down, down_bad


def test_adaptive_submit_escalate_and_docs_over_real_mcp(omo_home, tmp_path):
    with open(tmp_path / "stderr.log", "w") as errlog:
        expl, inv, bad_scope, low, docs_off, esc, esc_bad = asyncio.run(_adaptive_flow(errlog))
    assert expl == {"created": False, "roster_class": "none", "action": "answer_directly"}
    assert inv["created"] is True and inv["roster_class"] == "investigation"
    assert bad_scope.is_error and "invalid" in bad_scope.content[0].text, "unknown scope keys are rejected"
    assert low.is_error, "a class below the computed floor (security_sensitive -> heavy) is refused or ignored"
    assert docs_off.is_error and "docs_evidence is off" in docs_off.content[0].text
    assert esc.is_error, "an investigation job cannot be escalated into an editing team"
    assert esc_bad.is_error and "invalid" in esc_bad.content[0].text
