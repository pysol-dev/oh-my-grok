"""`omg hook serve`: a local-only, per-run session token web form for entering webhook URLs and keys.

For GUIs whose paste tool cannot type into a terminal. Security properties:
  - stdlib http.server bound to 127.0.0.1 on a random free port; every path except /setup/<token> is 404, where
    <token> is secrets.token_urlsafe(32) generated per run (compared in constant time);
  - every request must carry Host 127.0.0.1:<port> (and, if present, Origin http://127.0.0.1:<port>), so a DNS
    rebinding page cannot talk to it; anything else gets 403;
  - values arrive only in the POST body; they are sanitized (hooks.sanitize_input) and validated/stored by
    hooks.set_hook; they are never echoed in a page, never logged (request logging is disabled entirely), and never
    put into a URL; responses are no-store with a strict CSP;
  - the server stops on 'Finish', idle timeout, Ctrl-C (SIGINT) or SIGTERM, and then removes its URL file
    and any configured cleanup paths (e.g. the RUNBOOK redirect log)."""
from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .errors import OmgError
from .hooks import hook_status, sanitize_input, set_hook, url_diagnostic, validate_key, validate_url
from .paths import CODE_ROOT, Paths

FORM_ROLES = ("explorer", "librarian", "metis", "qa-executor", "momus", "code-reviewer", "executor")
MAX_BODY = 64 * 1024
URL_FILE = Path("/tmp/omg_hook_form_url")


def role_labels(engine_factory) -> dict[str, tuple[str, str]]:
    """role -> (bot name, routine name), from the active role bindings; falls back to config/roles.example.json,
    then to the omg-bus-<role> convention."""
    out: dict[str, tuple[str, str]] = {}
    example = {}
    try:
        ex = json.loads((CODE_ROOT / "config" / "roles.example.json").read_text())
        example = {b["role"]: b for b in ex.get("bindings", ex if isinstance(ex, list) else [])}
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        pass
    bound = {}
    try:
        e = engine_factory()
        try:
            bound = {b["role"]: b for b in e.list_bindings()}
        finally:
            e.close()
    except Exception:  # noqa: BLE001 - labels are cosmetic; fall back
        pass
    for r in FORM_ROLES:
        b, x = bound.get(r) or {}, example.get(r) or {}
        bot = b.get("bot_name") or x.get("bot_name") or "?"
        routine = b.get("routine_name") or b.get("routine_id") or x.get("routine_name") or f"omg-bus-{r}"
        out[r] = (bot, str(routine).split(" ")[0])
    return out


def store_pair(paths: Paths, engine_factory, role: str, raw_url: str, raw_key: str) -> tuple[str, str]:
    """Returns (status, safe message): skipped | configured | error. Never includes the values."""
    if not (raw_url or "").strip() and not (raw_key or "").strip():
        return "skipped", "left blank"
    url, url_removed = sanitize_input(raw_url)
    key, key_removed = sanitize_input(raw_key)
    if not url or not key:
        return "error", "both the URL and the key are needed (one was blank after cleanup)"
    try:
        validate_url(url)
    except OmgError as e:
        return "error", f"{e.message} ({url_diagnostic(url, url_removed)})"
    p = validate_key(key)
    if p:
        return "error", f"{p} (non_printable_or_paste_markers_removed={key_removed})"
    try:
        set_hook(paths, role, url, key)
        e = engine_factory()
        try:
            e.mark_hook_configured(role, True)
        finally:
            e.close()
    except OmgError as e:
        return "error", f"could not store: {e.message}"
    return "configured", "stored (0600); value not shown"


_CSS = ("body{font-family:sans-serif;max-width:46em;margin:1em auto;padding:0 1em}section{border:1px solid #ccc;"
        "padding:.5em 1em;margin:.7em 0}label{display:block;margin:.3em 0}input[type=password]{width:100%}"
        ".configured{color:#060}.error{color:#a00}.skipped{color:#666}")


def render(token: str, labels: dict, status: dict, results: dict | None = None, finished: bool = False) -> bytes:
    esc = html.escape
    parts = ["<!doctype html><html><head><meta charset='utf-8'><meta name='referrer' content='same-origin'>",
             f"<title>omg webhook setup</title><style>{_CSS}</style></head><body>",
             "<h1>omg webhook setup</h1>"]
    if finished:
        parts.append("<p><b>Finished. The setup server has stopped; you can close this tab.</b></p></body></html>")
        return "".join(parts).encode()
    parts.append("<p>Paste each routine's webhook URL and key. Fields are hidden and never shown back. Leave both "
                 "fields of a role blank to skip it. You can submit again to fix failures.</p>")
    if results:
        parts.append("<h2>Results</h2><ul>")
        for r in FORM_ROLES:
            if r in results:
                st, msg = results[r]
                parts.append(f"<li class='{esc(st)}'><b>{esc(r)}</b>: {esc(st)} &mdash; {esc(msg)}</li>")
        parts.append("</ul>")
    parts.append(f"<form method='post' action='/setup/{esc(token)}' autocomplete='off'>")
    for r in FORM_ROLES:
        bot, routine = labels[r]
        parts.append(
            f"<section><h3>{esc(r)} &mdash; bot {esc(bot)}, routine {esc(routine)}</h3>"
            f"<p>Open {esc(bot)}'s info pane &gt; Routines &gt; {esc(routine)}. "
            f"Configured already: <b>{'yes' if status.get(r) else 'no'}</b></p>"
            f"<label>Webhook URL <input type='password' name='url_{esc(r)}' autocomplete='off' "
            f"spellcheck='false' autocapitalize='off'></label>"
            f"<label>Webhook key <input type='password' name='key_{esc(r)}' autocomplete='off' "
            f"spellcheck='false' autocapitalize='off'></label></section>")
    parts.append("<p><button type='submit' name='action' value='save'>Save filled-in roles</button></p></form>")
    parts.append(f"<form method='post' action='/setup/{esc(token)}'><button type='submit' name='action' "
                 f"value='finish'>Finish (stop the setup server)</button></form></body></html>")
    return "".join(parts).encode()


class HookFormServer:
    def __init__(self, paths: Paths, engine_factory, idle_seconds: float = 1800.0, port: int = 0,
                 url_file: Path | None = None, cleanup_paths: list[Path] | None = None):
        self.paths, self.engine_factory, self.idle_seconds = paths, engine_factory, idle_seconds
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.finished = threading.Event()
        self.url_file = url_file
        self.cleanup_paths = list(cleanup_paths or [])
        self._stop_reason = "finished"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "omg-setup"
            sys_version = ""
            timeout = 30

            def log_message(self, *a, **k):  # never log (paths carry the token; bodies carry secrets)
                pass

            def log_request(self, *a, **k):
                pass

            def log_error(self, *a, **k):
                pass

            def _send(self, code: int, body: bytes = b""):
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "same-origin")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                                            "form-action 'self'; frame-ancestors 'none'")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _gate(self) -> bool:
                host = f"127.0.0.1:{outer.port}"
                if self.headers.get("Host") != host or \
                        self.headers.get("Origin") not in (None, f"http://{host}"):
                    self._send(403, b"forbidden")
                    return False
                if not hmac.compare_digest(self.path.encode(), f"/setup/{outer.token}".encode()):
                    self._send(404, b"not found")
                    return False
                outer.last_activity = time.monotonic()
                return True

            def do_GET(self):
                if self._gate():
                    self._send(200, render(outer.token, outer.labels(), hook_status(outer.paths)))

            def do_POST(self):
                if not self._gate():
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = -1
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if n < 0 or n > MAX_BODY or ctype != "application/x-www-form-urlencoded":
                    self._send(400, b"bad request")
                    return
                try:
                    form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True,
                                    max_num_fields=64)
                except ValueError:  # too many fields; never echo the body
                    self._send(400, b"bad request")
                    return
                if form.get("action", [""])[0] == "finish":
                    self._send(200, render(outer.token, {}, {}, finished=True))
                    outer.finished.set()
                    return
                results = {}
                with outer.lock:
                    for r in FORM_ROLES:
                        results[r] = store_pair(outer.paths, outer.engine_factory, r,
                                                form.get(f"url_{r}", [""])[0], form.get(f"key_{r}", [""])[0])
                del form
                self._send(200, render(outer.token, outer.labels(), hook_status(outer.paths), results))

            do_HEAD = do_GET

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/setup/{self.token}"

    def labels(self):
        return role_labels(self.engine_factory)

    def write_url_file(self):
        if self.url_file is None:
            return
        try:
            if self.url_file.is_symlink():
                self.url_file.unlink()
        except OSError:
            pass
        tmp = self.url_file.with_name(f".{self.url_file.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (self.url + "\n").encode())
        finally:
            os.close(fd)
        os.replace(tmp, self.url_file)  # readers never see an empty or partial file

    def cleanup_files(self) -> None:
        """Remove the URL file and any extra cleanup paths (e.g. redirect logs). Best-effort."""
        targets = []
        if self.url_file is not None:
            targets.append(self.url_file)
        targets.extend(self.cleanup_paths)
        for path in targets:
            try:
                path.unlink()
            except OSError:
                pass

    def _request_stop(self, reason: str) -> None:
        self._stop_reason = reason
        self.finished.set()
        # shutdown must not run on the signal thread; schedule it
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def arm_signal_handlers(self) -> dict:
        """Install SIGINT/SIGTERM handlers before the URL file is published. Returns previous handlers."""
        self._stop_reason = "finished"

        def _on_signal(signum, _frame):
            name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
            self._request_stop(name)

        prev = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev[sig] = signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                # not in main thread, or unsupported — skip; Finish/idle still work
                pass
        self._prev_signal_handlers = prev
        return prev

    def _watch(self):
        while not self.finished.wait(1.0):
            if time.monotonic() - self.last_activity > self.idle_seconds:
                self._stop_reason = "idle timeout"
                self.finished.set()
        time.sleep(0.3)  # let the 'Finished' page go out
        self.httpd.shutdown()

    def serve(self) -> str:
        """Blocks until Finish, idle timeout, SIGINT or SIGTERM. Returns the reason."""
        prev = getattr(self, "_prev_signal_handlers", None)
        if prev is None:
            prev = self.arm_signal_handlers()
        self.write_url_file()
        w = threading.Thread(target=self._watch, daemon=True)
        w.start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            for sig, handler in prev.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            self._prev_signal_handlers = None
            self.httpd.server_close()
            self.cleanup_files()
        if self._stop_reason in ("SIGINT", "SIGTERM", "idle timeout"):
            return self._stop_reason
        return "finished" if time.monotonic() - self.last_activity <= self.idle_seconds else "idle timeout"
