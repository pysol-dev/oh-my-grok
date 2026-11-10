"""Engine-side Context7 connector (spec/ADAPTIVE.md section 8, R10).

The ENGINE makes these MCP calls; a bot's statement that it consulted Context7 is never evidence. A successful call
yields a transcript (endpoint, server identity, each tool call's arguments and verbatim response) that the engine
stores and hashes. Nothing here fabricates a quote, a library id or a response."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .redact import redact

LIB_ID_RE = re.compile(r"Context7-compatible library ID:\s*(/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+)")
TITLE_RE = re.compile(r"Title:\s*(.+)")
MAX_RESPONSE = 400_000


class Context7Error(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # connect | tool_error | no_library | timeout | protocol


@dataclass
class Context7Result:
    endpoint: str
    server: dict
    protocol_version: str | None
    library_id: str
    library_title: str | None
    calls: list[dict] = field(default_factory=list)
    text: str = ""


def _text(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "text", None) is not None)[:MAX_RESPONSE]


async def _run(url: str, library_name: str | None, query: str, library_id: str | None, api_key: str | None,
               timeout: float) -> Context7Result:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout)) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                missing = {"resolve-library-id", "query-docs"} - tools
                if missing:
                    raise Context7Error(f"server does not offer {sorted(missing)}", "protocol")
                calls, title = [], None
                if library_id is None:
                    args = {"libraryName": library_name, "query": query}
                    res = await s.call_tool("resolve-library-id", args)
                    body = _text(res)
                    calls.append({"tool": "resolve-library-id", "arguments": args, "is_error": bool(res.is_error),
                                  "response": body})
                    if res.is_error:
                        raise Context7Error("resolve-library-id returned an error: " + redact(body)[:300], "tool_error")
                    m = LIB_ID_RE.search(body)
                    if not m:
                        raise Context7Error("no library matched; nothing was fetched", "no_library")
                    library_id = m.group(1)
                    tm = TITLE_RE.search(body)
                    title = tm.group(1).strip() if tm else None
                args = {"libraryId": library_id, "query": query}
                res = await s.call_tool("query-docs", args)
                body = _text(res)
                calls.append({"tool": "query-docs", "arguments": args, "is_error": bool(res.is_error), "response": body})
                if res.is_error or not body.strip():
                    raise Context7Error("query-docs returned " + ("an error: " + redact(body)[:300] if res.is_error
                                                                  else "an empty response"), "tool_error")
                si = init.server_info
                return Context7Result(endpoint=url, server={"name": si.name, "version": si.version},
                                      protocol_version=str(init.protocol_version), library_id=library_id,
                                      library_title=title, calls=calls, text=body)


def _first_leaf(e: BaseException) -> BaseException:
    """anyio task groups wrap errors raised inside the client context in (nested) ExceptionGroups."""
    while getattr(e, "exceptions", None):
        subs = e.exceptions
        own = [x for x in subs if isinstance(x, Context7Error)]
        e = own[0] if own else subs[0]
    return e


def fetch(url: str, query: str, library_name: str | None = None, library_id: str | None = None,
          api_key: str | None = None, timeout: float = 30.0) -> Context7Result:
    if not query or not query.strip():
        raise Context7Error("query is required", "protocol")
    if not (library_name or library_id):
        raise Context7Error("library_name or library_id is required", "protocol")
    if library_id is not None and not LIB_ID_RE.fullmatch(f"Context7-compatible library ID: {library_id}"):
        raise Context7Error("library_id must look like /org/project", "protocol")
    try:
        return asyncio.run(asyncio.wait_for(_run(url, library_name, query, library_id, api_key, timeout),
                                            timeout=timeout * 2 + 5))
    except Context7Error:
        raise
    except asyncio.TimeoutError as e:
        raise Context7Error(f"timed out after {timeout}s", "timeout") from e
    except BaseException as e:  # noqa: BLE001 - anyio ExceptionGroups, httpx errors, protocol errors
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        e = _first_leaf(e)
        if isinstance(e, Context7Error):
            raise e
        msg = f"{type(e).__name__}: {e}"
        if api_key:
            msg = msg.replace(api_key, "[REDACTED]")
        raise Context7Error("cannot reach Context7: " + redact(msg)[:300], "connect") from e
