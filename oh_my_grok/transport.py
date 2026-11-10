"""HTTP POST for webhook wake-ups: bounded connect/read timeouts, no secrets in errors."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from .redact import redact


@dataclass
class HttpResult:
    status: int | None          # None = network failure / uncertain
    retry_after: float | None
    error: str | None


def post_json(url: str, key: str, body: dict, connect_timeout: float, read_timeout: float) -> HttpResult:
    u = urlparse(url)
    data = json.dumps(body, separators=(",", ":")).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "User-Agent": "oh-my-grok-dispatcher/1"}
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=connect_timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=connect_timeout)
        conn.connect()
        conn.sock.settimeout(read_timeout)
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        resp.read(4096)
        ra = resp.getheader("Retry-After")
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
        conn.close()
        return HttpResult(resp.status, retry_after, None)
    except (OSError, socket.timeout, http.client.HTTPException, ssl.SSLError) as e:
        return HttpResult(None, None, redact(f"{type(e).__name__}: {e}")[:300])
