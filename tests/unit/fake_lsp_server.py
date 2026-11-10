"""Scripted LSP server for client tests. argv[1] selects the scenario."""
import json
import os
import sys
import time

scenario = sys.argv[1]
inp, out = sys.stdin.buffer, sys.stdout.buffer


def send(obj):
    b = json.dumps(obj).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(b) + b)
    out.flush()


def read():
    n = None
    while True:
        line = inp.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":")[1])
    return json.loads(inp.read(n))


def diag(msg, code="E1", sev=1):
    return {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "severity": sev,
            "code": code, "message": msg}


backlog = []


def read_response(rid):
    while True:
        m = read()
        if m.get("id") == rid and "method" not in m:
            return m
        backlog.append(m)


caps = {"diagnosticProvider": {"interFileDependencies": False, "workspaceDiagnostics": False}} \
    if scenario.startswith("pull") else {}
seen_cfg = None
while True:
    m = backlog.pop(0) if backlog else read()
    meth, mid = m.get("method"), m.get("id")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": caps, "serverInfo": {"name": "fake", "version": "1"}}})
    elif meth == "initialized":
        if scenario == "config_request":
            send({"jsonrpc": "2.0", "id": 900, "method": "workspace/configuration",
                  "params": {"items": [{"section": "a.b"}, {"section": "missing"}]}})
            seen_cfg = read_response(900 if scenario == 'config_request' else 901)
        if scenario == "unknown_request":
            send({"jsonrpc": "2.0", "id": 901, "method": "foo/bar", "params": {}})
            seen_cfg = read_response(900 if scenario == 'config_request' else 901)
    elif meth == "textDocument/didOpen":
        td = m["params"]["textDocument"]
        uri, ver = td["uri"], td["version"]
        if scenario == "push_versioned":
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "version": ver - 1, "diagnostics": [diag("stale")]}})
            time.sleep(0.05)
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "version": ver, "diagnostics": [diag("fresh", "E2")]}})
        elif scenario == "push_unversioned":
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "diagnostics": []}})
            time.sleep(0.1)
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "diagnostics": [diag("late", "E3")]}})
        elif scenario == "crash_on_open":
            os._exit(3)
        elif scenario == "malformed":
            out.write(b"Content-Length: abc\r\n\r\n{}")
            out.flush()
        elif scenario == "oversize":
            out.write(b"Content-Length: 999999999\r\n\r\n")
            out.flush()
        elif scenario == "badjson":
            out.write(b"Content-Length: 5\r\n\r\n{nope")
            out.flush()
        elif scenario == "flood":
            for i in range(200):
                send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                      "params": {"uri": uri, "diagnostics": [diag(f"n{i}")]}})
                time.sleep(0.01)
    elif meth == "textDocument/diagnostic":
        if scenario == "pull_unchanged":
            send({"jsonrpc": "2.0", "id": mid, "result": {"kind": "unchanged", "resultId": "x"}})
        elif scenario == "pull_error":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32803, "message": "boom"}})
        elif scenario == "pull_hang":
            pass
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"kind": "full", "items": [diag("pulled", "P1", 2)]}})
    elif meth == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif meth == "exit":
        sys.exit(0)
    elif meth == "textDocument/didClose" and scenario in ("config_request", "unknown_request"):
        pass
    if scenario in ("config_request", "unknown_request") and meth == "textDocument/didOpen":
        td = m["params"]["textDocument"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": td["uri"], "version": td["version"],
                         "diagnostics": [diag(json.dumps(seen_cfg, sort_keys=True))]}})
