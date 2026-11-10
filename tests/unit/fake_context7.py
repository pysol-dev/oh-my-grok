"""A local MCP server speaking real streamable-HTTP MCP with Context7's two tool names (no network)."""
import json
import os
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

MODE = os.environ.get("FAKE_C7_MODE", "ok")
LOG = os.environ.get("FAKE_C7_LOG")
DOC = ("### Widget retry policy\n\nSource: https://example.invalid/widgets\n\n"
       "Widget.configure(retries=3) sets the retry count. The default backoff is exponential with jitter.\n")

app = MCPServer("FakeContext7", version="9.9.9")


@app.tool(name="resolve-library-id")
def resolve(libraryName: str, query: str) -> str:
    if MODE == "no_library":
        return "No libraries found."
    if MODE == "error_resolve":
        raise RuntimeError("resolver exploded")
    return (f"Available Libraries:\n\n- Title: {libraryName.title()}\n"
            f"- Context7-compatible library ID: /acme/{libraryName.lower()}\n- Description: fake\n")


@app.tool(name="query-docs")
def query(libraryId: str, query: str) -> str:
    if MODE == "error_query":
        raise RuntimeError("docs backend down")
    if MODE == "empty":
        return "   "
    return DOC + f"\n(library {libraryId}; query {query})\n"


class Spy:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if LOG and scope["type"] == "http":
            hdrs = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            with open(LOG, "a") as f:
                f.write(json.dumps({"auth": hdrs.get("authorization")}) + "\n")
        await self.inner(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(Spy(app.streamable_http_app()), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
