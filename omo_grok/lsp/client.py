"""Minimal, strict LSP client: Content-Length framed JSON-RPC over a child's stdio.

Bounded everywhere (message size, wait times, stderr capture) and fail-closed: a crash, a malformed frame, an
oversized message or a missing answer raises ``LspError`` and is never read as "no diagnostics"."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Callable

MAX_MESSAGE = 16 << 20
MAX_HEADER = 4096
STDERR_KEEP = 32 << 10


class LspError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # timeout | crash | protocol | server_error | oversize


class MethodNotFound(Exception):
    pass


class LspClient:
    def __init__(self, argv: list[str], env: dict | None = None, cwd: str | None = None, preexec=None,
                 request_handler: Callable[[str, object], object] | None = None, max_message: int = MAX_MESSAGE):
        self.argv, self.env, self.cwd, self.preexec = argv, env, cwd, preexec
        self.request_handler = request_handler
        self.max_message = max_message
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()          # guards writes and the shared tables below
        self._cond = threading.Condition(self._lock)
        self._next_id = 1
        self._pending: dict[int, dict] = {}
        self._published: dict[str, list[dict]] = {}   # uri -> [{version, diagnostics, at, seq}]
        self._seq = 0
        self.closed: LspError | None = None
        self._stderr = bytearray()
        self.log_messages: list[str] = []
        self.server_requests: list[str] = []
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=self.env, cwd=self.cwd, shell=False,
                                         preexec_fn=self.preexec, close_fds=True)
        except OSError as e:
            raise LspError(f"cannot start language server: {type(e).__name__}: {e}", "crash") from e
        for target in (self._read_loop, self._stderr_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.closed is None

    def stderr_tail(self) -> str:
        return bytes(self._stderr[-4096:]).decode("utf-8", "replace")

    def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            chunk = self.proc.stderr.read1(4096) if hasattr(self.proc.stderr, "read1") else self.proc.stderr.read(4096)
            if not chunk:
                return
            self._stderr += chunk
            if len(self._stderr) > STDERR_KEEP:
                del self._stderr[:len(self._stderr) - STDERR_KEEP]

    def _fail(self, err: LspError) -> None:
        with self._cond:
            if self.closed is None:
                self.closed = err
            self._cond.notify_all()

    def close(self, graceful_timeout: float = 3.0) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if self.closed is None:
                    self.request("shutdown", None, timeout=graceful_timeout)
                    self.notify("exit", None)
            except LspError:
                pass
            try:
                proc.wait(timeout=graceful_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for f in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=2)

    # ------------------------------------------------------------------ framing
    def _send(self, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        with self._lock:
            if self.closed is not None:
                raise self.closed
            try:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(frame)
                self.proc.stdin.flush()
            except (OSError, ValueError, AssertionError) as e:
                raise LspError(f"write to language server failed: {type(e).__name__}", "crash") from e

    def _read_exact(self, n: int) -> bytes:
        assert self.proc and self.proc.stdout
        buf = bytearray()
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def _read_message(self) -> dict:
        assert self.proc and self.proc.stdout
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            b = self.proc.stdout.read(1)
            if not b:
                raise EOFError
            header += b
            if len(header) > MAX_HEADER:
                raise LspError("language server header too large", "protocol")
        length = None
        for line in header.decode("ascii", "replace").split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    raise LspError("malformed Content-Length", "protocol") from None
        if length is None or length < 0:
            raise LspError("missing Content-Length", "protocol")
        if length > self.max_message:
            raise LspError(f"language server message of {length} bytes exceeds the {self.max_message} limit",
                           "oversize")
        body = self._read_exact(length)
        try:
            msg = json.loads(body)
        except ValueError:
            raise LspError("language server sent invalid JSON", "protocol") from None
        if not isinstance(msg, dict):
            raise LspError("language server sent a non-object message", "protocol")
        return msg

    def _read_loop(self) -> None:
        try:
            while True:
                self._dispatch(self._read_message())
        except EOFError:
            rc = self.proc.poll() if self.proc else None
            self._fail(LspError(f"language server closed its output (exit {rc}): {self.stderr_tail()[-300:]}", "crash"))
        except LspError as e:
            self._fail(e)
        except Exception as e:  # noqa: BLE001
            self._fail(LspError(f"client reader failed: {type(e).__name__}: {e}", "protocol"))

    def _dispatch(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method is None and mid is not None:  # response
            with self._cond:
                slot = self._pending.get(mid)
                if slot is not None:
                    slot["msg"] = msg
                self._cond.notify_all()
            return
        if method is not None and mid is not None:  # server -> client request
            self.server_requests.append(method)
            try:
                if self.request_handler is None:
                    raise MethodNotFound(method)
                result = self.request_handler(method, msg.get("params"))
                self._send({"jsonrpc": "2.0", "id": mid, "result": result})
            except MethodNotFound:
                self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
            return
        if method == "textDocument/publishDiagnostics":
            p = msg.get("params") or {}
            with self._cond:
                self._seq += 1
                self._published.setdefault(p.get("uri"), []).append(
                    {"version": p.get("version"), "diagnostics": p.get("diagnostics") or [], "at": time.monotonic(),
                     "seq": self._seq})
                self._cond.notify_all()
        elif method in ("window/logMessage", "window/showMessage"):
            if len(self.log_messages) < 200:
                self.log_messages.append(str((msg.get("params") or {}).get("message", ""))[:300])

    # ------------------------------------------------------------------ requests
    def request(self, method: str, params, timeout: float = 30.0):
        with self._cond:
            if self.closed is not None:
                raise self.closed
            rid = self._next_id
            self._next_id += 1
            self._pending[rid] = {}
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            with self._cond:
                while "msg" not in self._pending[rid]:
                    if self.closed is not None:
                        raise self.closed
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise LspError(f"timed out after {timeout}s waiting for {method}", "timeout")
                    self._cond.wait(left)
                msg = self._pending[rid]["msg"]
        finally:
            with self._cond:
                self._pending.pop(rid, None)
        if "error" in msg:
            e = msg["error"] or {}
            raise LspError(f"{method} failed: {e.get('code')} {str(e.get('message'))[:300]}", "server_error")
        return msg.get("result")

    def notify(self, method: str, params) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    # ------------------------------------------------------------------ push diagnostics
    def wait_push(self, uri: str, version: int | None, quiet: float, deadline: float) -> list[dict]:
        """Diagnostics for ``uri`` once the server has been silent for ``quiet`` seconds after its first matching
        message. A message that carries a ``version`` must carry the one we sent. Reaching the deadline, or the
        server dying, is a failure, never an empty result."""
        end = time.monotonic() + deadline
        with self._cond:
            while True:
                if self.closed is not None:
                    raise self.closed
                msgs = [m for m in self._published.get(uri, [])
                        if m["version"] is None or version is None or m["version"] == version]
                now = time.monotonic()
                if msgs:
                    last = msgs[-1]
                    if now - last["at"] >= quiet:
                        return last["diagnostics"]
                    wake = last["at"] + quiet - now
                else:
                    wake = end - now
                if end - now <= 0:
                    raise LspError(f"no stable diagnostics for {uri} within {deadline}s", "timeout")
                self._cond.wait(max(0.005, min(wake, end - now)))
